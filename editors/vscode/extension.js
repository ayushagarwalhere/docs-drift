const vscode = require('vscode');
const { LanguageClient } = require('vscode-languageclient/node');
let client;
exports.activate = async function (context) {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!root) return;
  const command = vscode.workspace.getConfiguration('docsDrift').get('executable');
  const watcher = vscode.workspace.createFileSystemWatcher('**/*.{py,js,ts,tsx,jsx,json,toml,yml,yaml}');
  context.subscriptions.push(watcher);
  client = new LanguageClient('docs-drift', 'Documentation Drift',
    { command, args: ['lsp', '--repo', root] },
    { documentSelector: [{ scheme: 'file', language: 'markdown' }, { scheme: 'file', language: 'mdx' },
                         { scheme: 'file', language: 'restructuredtext' }],
      synchronize: { fileEvents: watcher } });
  await client.start();
};
exports.deactivate = () => client?.stop();
