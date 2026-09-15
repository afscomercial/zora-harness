#!/usr/bin/env python3
"""Run one test command using its committed CI environment, inside the QA sandbox."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG = '.circleci/services.json'
TEMPLATE = '.circleci/templates/job-definitions.yml'
PROTECTED = {'HOME', 'PATH', 'KUBECONFIG', 'CODEX_HOME', 'PLAYWRIGHT_BROWSERS_PATH'}


def shared_test_environment(raw):
    """Parse only the committed primary test-container scalar mapping; no YAML execution."""
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError:
        raise ValueError('CI template is not UTF-8') from None
    starts = [i for i, line in enumerate(lines) if line in ('  test:', '  test-service:')]
    if len(starts) != 1:
        raise ValueError('CI template must contain one recognized test job')
    start = starts[0] + 1
    end = next((i for i in range(start, len(lines))
                if lines[i].strip() and not lines[i].lstrip().startswith('#')
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    block = lines[start:end]
    dockers = [i for i, line in enumerate(block) if line == '    docker:']
    if len(dockers) != 1:
        raise ValueError('CI test job must contain one docker block')
    content = [line for line in block[dockers[0] + 1:] if line.strip() and not line.lstrip().startswith('#')]
    if len(content) < 3 or not re.fullmatch(r'      - image: [^\s]+', content[0]) or content[1] != '        environment:':
        raise ValueError('CI primary-container environment structure changed')
    values = {}
    for line in content[2:]:
        indent = len(line) - len(line.lstrip())
        if indent < 10:
            break
        match = re.fullmatch(r'          ([A-Za-z_][A-Za-z0-9_]*): (.+)', line)
        if not match or match[1] in values:
            raise ValueError('CI shared environment must be a unique flat scalar mapping')
        key, value = match.groups()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError('unsupported quoted CI scalar') from None
            if not isinstance(value, str):
                raise ValueError('CI quoted scalar must be a string')
        elif value.startswith("'"):
            if not re.fullmatch(r"'(?:[^']|'')*'", value):
                raise ValueError('unsupported single-quoted CI scalar')
            value = value[1:-1].replace("''", "'")
        elif (any(c.isspace() for c in value) or value[0] in '&*!{|[>\"' or '#' in value
              or value.lower() in ('null', '~', '.nan', '.inf', '-.inf')):
            raise ValueError('unsupported CI scalar syntax')
        values[key] = value
    if not values:
        raise ValueError('CI primary-container environment is empty')
    # The helper does not provision CI's standalone replica-set sidecar. Keep the
    # existing memory-server fallback unless the caller explicitly supplies its URI.
    values.pop('MONGO_TEST_URI', None)
    return values


def child_environment(workspace, parent):
    if parent.get('QA_SANDBOX') != '1':
        raise ValueError('QA_SANDBOX=1 is required')
    work = parent.get('QA_WORK')
    if not work or not Path(work).is_absolute():
        raise ValueError('QA_WORK must identify the absolute checkout root')
    work = str(Path(work).resolve())
    root = subprocess.run(['git', '-C', work, 'rev-parse', '--show-toplevel'],
                          capture_output=True, text=True, check=False)
    if root.returncode or str(Path(root.stdout.strip()).resolve()) != work:
        raise ValueError('QA_WORK must be the checkout root')
    result = subprocess.run(['git', '-C', work, 'show', 'HEAD:' + CONFIG],
                            capture_output=True, check=False)
    if result.returncode:
        raise ValueError('committed CI service configuration is unavailable')
    try:
        config = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError('committed CI service configuration is invalid JSON') from None
    if not isinstance(config, dict) or not isinstance(config.get('services'), list):
        raise ValueError('CI configuration must contain a services list')
    matches = []
    for service in config['services']:
        if not isinstance(service, dict) or any(not isinstance(service.get(key), str) or not service[key]
                                                for key in ('name', 'packageName', 'path')):
            raise ValueError('CI service identity is malformed')
        if workspace in (service['name'], service['packageName'], service['path']):
            matches.append(service)
    if len(matches) != 1:
        raise ValueError('workspace must match exactly one CI service')
    selected = matches[0]
    variables = selected.get('testEnvVars')
    if not isinstance(variables, dict):
        raise ValueError('selected service testEnvVars must be an object')
    template = subprocess.run(['git', '-C', work, 'show', 'HEAD:' + TEMPLATE],
                              capture_output=True, check=False)
    if template.returncode:
        raise ValueError('committed CI job template is unavailable')
    shared = shared_test_environment(template.stdout)
    overlay = {}
    # Validate both maps before merging; even an overridden protected key is invalid.
    for key, value in list(shared.items()) + list(variables.items()):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('testEnvVars contains an invalid variable name')
        if key in PROTECTED or key.startswith(('QA_', 'DOCKER_', 'XDG_')):
            raise ValueError('testEnvVars attempts to override a protected control variable: ' + key)
        if isinstance(value, bool):
            converted = str(value).lower()
        elif isinstance(value, str):
            converted = value
        elif isinstance(value, (int, float)) and (not isinstance(value, float) or math.isfinite(value)):
            converted = str(value)
        else:
            raise ValueError('testEnvVars values must be strings, finite numbers or booleans: ' + key)
        if '\0' in converted:
            raise ValueError('testEnvVars contains an invalid value: ' + key)
        overlay[key] = converted
    digest = hashlib.sha256(CONFIG.encode() + b'\0' + result.stdout + TEMPLATE.encode() + b'\0' + template.stdout).hexdigest()
    return work, parent | overlay, selected['name'], digest, sorted(overlay)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print('usage: qa-test-env.py WORKSPACE COMMAND [ARGS...]', file=sys.stderr)
        return 2
    work, environment, selected, digest, names = child_environment(args[0], dict(os.environ))
    print(json.dumps({'workspace': selected, 'config_sha256': digest, 'variable_names': names}), flush=True)
    result = subprocess.run(args[1:], cwd=work, env=environment, check=False)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError):
        # Avoid reflecting configuration values or command arguments into diagnostics.
        print('QA test environment refused: sandbox, checkout, service configuration or command is invalid', file=sys.stderr)
        raise SystemExit(2)
