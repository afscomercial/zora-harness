// Idempotent baseline only. No borrowers, loans, or production database copies.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { randomUUID } = require('node:crypto');
const work = process.env.QA_WORK;
const uri = process.env.QA_MONGO_URI;
if (!work || !uri || !process.env.QA_IDENTITY_FILE) throw Error('Missing QA seed configuration');
const { MongoClient } = createRequire(path.join(work, 'package.json'))('mongodb');
const identity = JSON.parse(fs.readFileSync(process.env.QA_IDENTITY_FILE, 'utf8'));
if (!identity.user?.clerkUserId || !identity.user?.userId || !identity.tenant?.clerkOrgId ||
    identity.user.tenantId !== identity.tenant.tenantId) throw Error('Incomplete or inconsistent Clerk mapping');
// System users are tenant configuration, kept in the VM's identity file rather than this public repo.
if (!Array.isArray(identity.systemUsers) || !identity.systemUsers.every(u => u?.email && u.firstName && u.lastName))
  throw Error('identity.systemUsers must list the system users ({email, firstName, lastName}) from the seed-local-db skill');
const client = new MongoClient(uri, { serverSelectionTimeoutMS: 10000 });
async function upsert(collection, key, fields) {
  await collection.updateOne(key, { $set: {...fields, updatedAt: new Date()},
    $setOnInsert: {createdAt: new Date(), __v: 0} }, {upsert:true});
}
(async () => {
  try {
    await client.connect();
    const users = client.db('users');
    await upsert(users.collection('tenants'), {tenantId:identity.tenant.tenantId}, identity.tenant);
    await upsert(users.collection('users'), {clerkUserId:identity.user.clerkUserId}, identity.user);
    for (const {email,firstName,lastName} of identity.systemUsers) {
      await users.collection('users').updateOne({tenantId:identity.tenant.tenantId,'attributes.email':email}, {
        $set: {attributes:{email,firstName,lastName,role:'user',userType:'system',status:'active',preferences:{}}, updatedAt:new Date()},
        $setOnInsert:{userId:randomUUID(),tenantId:identity.tenant.tenantId,createdAt:new Date(),__v:0}
      },{upsert:true});
    }
    await upsert(client.db('underwriting-guidelines-service').collection('underwritingGuidelines'),
      {underwritingGuidelineId:'9b4c1e5d-0000-4000-8000-fnmasellinggde'},
      {underwritingGuidelineId:'9b4c1e5d-0000-4000-8000-fnmasellinggde',attributes:{name:'FNMA Selling Guide'}});
    const user=await users.collection('users').findOne({clerkUserId:identity.user.clerkUserId});
    if (user.userId !== identity.user.userId || user.tenantId !== identity.tenant.tenantId) throw Error('Seed verification failed');
    console.log(`Seed verified: QA user, matching Clerk tenant, ${identity.systemUsers.length} system user(s), FNMA Selling Guide.`);
  } finally { await client.close(); }
})().catch(e=>{console.error(e.message);process.exitCode=1});
