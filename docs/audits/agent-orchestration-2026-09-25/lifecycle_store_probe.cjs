const fs = require('fs');
const vm = require('vm');
const { createRequire } = require('module');
const root = require('path').resolve(__dirname, '../../..');
const realRequire = createRequire(root + '/package.json');
const ts = realRequire('typescript');
const source = fs.readFileSync(root + '/frontend/src/store/agentChatStore.ts', 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
let releaseDispatch;
const calls = [];
const service = {
  streamMessage: async (request, callbacks) => {
    calls.push(['stream', request]);
    callbacks.onToken('partial answer');
    throw new Error('connection lost');
  },
  startDurableRun: async request => {
    calls.push(['durable', request]);
    return new Promise(resolve => { releaseDispatch = resolve; });
  },
};
const mod = {exports:{}};
const sandbox = {module:mod,exports:mod.exports,console,AbortController,setTimeout,clearTimeout,
  require: id => {
    if(id === '@/services/agentChatService') return {agentChatService:service};
    if(id === '@/components/context-rail/toolLabels') return {toolLabel:x=>x};
    if(id === '@/lib/query-client') return {getAppQueryClient:()=>undefined};
    return realRequire(id);
  }
};
vm.runInNewContext(compiled, sandbox, {filename:'agentChatStore.js'});
const store = mod.exports.useAgentChatStore;
(async()=>{
  store.getState().setInputValue('hello');
  const sending = store.getState().sendMessage();
  await new Promise(resolve=>setImmediate(resolve));
  console.log('After transport failure requests:', calls.map(x=>x[0]));
  console.log('Client message id:', calls[0][1].messages.at(-1).client_message_id);
  store.getState().newThread();
  console.log('Blank new thread message count:', store.getState().messages.length);
  releaseDispatch({runId:'job-after-new-thread'});
  await sending;
  console.log('After obsolete dispatch returns:', store.getState().messages.map(x=>({role:x.role,content:x.content,isStreaming:x.isStreaming})));
  console.log('Current thread streaming:', store.getState().isStreaming);
})();
