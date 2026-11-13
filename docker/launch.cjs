'use strict';
// stdin is the only transport for credentials and prompt. Never log either.
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

function exactKeys(obj, allowed) {
  return obj !== null && typeof obj === 'object' && !Array.isArray(obj)
    && Object.keys(obj).every(key => allowed.includes(key));
}
function validRefs(refs) {
  return Array.isArray(refs) && refs.every(ref => typeof ref === 'string'
    && /^(evt|mem)_[A-Za-z0-9]{8,40}$/.exec(ref)?.[0] === ref)
    && new Set(refs).size === refs.length;
}
function validate(envelope) {
  if (!exactKeys(envelope, ['auth', 'prompt', 'citation_refs']) || typeof envelope.prompt !== 'string'
      || !validRefs(envelope.citation_refs)
      || !envelope.prompt.trim() || !exactKeys(envelope.auth, ['auth_mode', 'tokens', 'last_refresh'])
      || envelope.auth.auth_mode !== 'chatgpt'
      || !exactKeys(envelope.auth.tokens, ['access_token', 'id_token', 'refresh_token', 'account_id'])) {
    throw new Error('invalid transport');
  }
  const tokens = envelope.auth.tokens;
  for (const key of ['access_token', 'id_token', 'refresh_token']) {
    if (typeof tokens[key] !== 'string' || !tokens[key].trim()) throw new Error('invalid credentials');
  }
  if ('account_id' in tokens && (typeof tokens.account_id !== 'string' || !tokens.account_id.trim())) {
    throw new Error('invalid account');
  }
  if ('last_refresh' in envelope.auth && (typeof envelope.auth.last_refresh !== 'string'
      || !Number.isFinite(Date.parse(envelope.auth.last_refresh)))) throw new Error('invalid refresh time');
  return envelope;
}
function citationSchema(refs) {
  if (!validRefs(refs)) throw new Error('invalid citations');
  // This installed template is the only schema source; stdin cannot supply a schema or path.
  const schema = JSON.parse(fs.readFileSync(path.join(__dirname, 'response.schema.json'), 'utf8'));
  const slots = schema.properties.proposals.items.anyOf.map(item => item.properties.cites);
  slots.push(schema.properties.attention.properties.cites);
  for (const slot of slots) {
    if (refs.length) slot.items.enum = [...refs];
    else slot.maxItems = 0;
  }
  return schema;
}
function scopedCommand(argv, schemaPath) {
  const index = argv.indexOf('--output-schema');
  if (index < 1 || index !== argv.lastIndexOf('--output-schema')
      || argv[index + 1] !== '/opt/iam/response.schema.json') throw new Error('invalid schema option');
  const command = [...argv];
  command[index + 1] = schemaPath;
  return command;
}
function returnedAuth(authPath) {
  if (fs.statSync(authPath).size > 65536) throw new Error('invalid credentials');
  const raw = JSON.parse(fs.readFileSync(authPath, 'utf8'));
  if (!exactKeys(raw, ['auth_mode', 'tokens', 'last_refresh', 'OPENAI_API_KEY']) || raw.OPENAI_API_KEY
      || !exactKeys(raw.tokens, ['access_token', 'id_token', 'refresh_token', 'account_id', 'login_owner_id', 'oauth_provenance'])) {
    throw new Error('invalid credentials');
  }
  const auth = {auth_mode: raw.auth_mode, tokens: {}};
  for (const key of ['access_token', 'id_token', 'refresh_token', 'account_id']) {
    if (key in raw.tokens) auth.tokens[key] = raw.tokens[key];
  }
  if ('last_refresh' in raw) auth.last_refresh = raw.last_refresh;
  return validate({auth, prompt: 'private credential handoff', citation_refs: []}).auth;
}
async function main(argv) {
  let authPath;
  let written = false;
  let schemaPath;
  let schemaWritten = false;
  let code = 1;
  try {
    if (!argv.length) throw new Error('missing command');
    const chunks = [];
    let bytes = 0;
    for await (const chunk of process.stdin) {
      bytes += chunk.length;
      if (bytes > 4 * 1024 * 1024) throw new Error('transport too large');
      chunks.push(chunk);
    }
    const envelope = validate(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    const home = process.env.CODEX_HOME || '/home/codex/.codex';
    fs.mkdirSync(home, {recursive: true, mode: 0o700});
    schemaPath = path.join(home, 'request.schema.json');
    const command = scopedCommand(argv, schemaPath);
    fs.writeFileSync(schemaPath, JSON.stringify(citationSchema(envelope.citation_refs)), {mode: 0o600, flag: 'wx'});
    schemaWritten = true;
    authPath = path.join(home, 'auth.json');
    fs.writeFileSync(authPath, JSON.stringify(envelope.auth), {mode: 0o600, flag: 'wx'});
    written = true;
    code = await new Promise((resolve, reject) => {
      const child = spawn(command[0], command.slice(1), {shell: false, stdio: ['pipe', 'pipe', 'ignore']});
      child.once('error', reject);
      child.stdout.pipe(process.stdout);
      child.stdin.on('error', () => {});
      child.stdin.end(envelope.prompt);
      child.once('close', (status) => resolve(status === null ? 1 : status));
    });
    if (code !== 0) process.stderr.write('Codex execution failed.\n');
  } catch {
    process.stderr.write('Codex bootstrap failed.\n');
    code = 1;
  } finally {
    if (schemaWritten) {
      try { fs.unlinkSync(schemaPath); }
      catch { /* Container tmpfs is removed by docker --rm as the final boundary. */ }
    }
    if (written) {
      if (process.env.IAM_AUTH_WRITEBACK === '1') {
        try {
          // Private transport, consumed before parsing the model stream. Never log it.
          process.stdout.write('\n' + JSON.stringify({type: 'iam.auth.updated', auth: returnedAuth(authPath)}) + '\n');
        } catch {
          process.stderr.write('Codex auth handoff failed.\n');
          code = 1;
        }
      }
      try { fs.unlinkSync(authPath); }
      catch { /* Container tmpfs is removed by docker --rm as the final boundary. */ }
    }
  }
  return code;
}
module.exports = {validate, citationSchema, main};
if (require.main === module) main(process.argv.slice(2)).then(code => { process.exitCode = code; });
