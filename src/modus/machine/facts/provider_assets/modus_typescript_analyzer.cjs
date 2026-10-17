/* Modus TypeScript observations. Only the installed compiler library executes;
 * source modules, language-service plugins and project lifecycle scripts do not.
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const request = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const ts = require(request.typescriptModule);
if (!ts.createProgram || !ts.createCompilerHost || !ts.isCallExpression || Number(String(ts.version).split(".")[0]) >= 7) {
  throw new Error("typescript.unsupported-compiler-api");
}
const root = fs.realpathSync(request.root);
const compilerRoot = fs.realpathSync(path.dirname(request.typescriptModule));
const sourceTexts = new Map(request.files.map(row => [path.resolve(root, row.path), row.text]));
const stable = (kind, ...parts) => kind + ":" + crypto.createHash("sha256").update(parts.join("\x1f")).digest("hex").slice(0, 24);
const under = (file, directory) => file === directory || file.startsWith(directory + path.sep);
function localPath(file) {
  try {
    const resolved = fs.realpathSync(file);
    return under(resolved, root) || under(resolved, compilerRoot) ? resolved : undefined;
  } catch { return undefined; }
}
function read(file) {
  const absolute = path.resolve(file);
  if (sourceTexts.has(absolute)) return sourceTexts.get(absolute);
  const resolved = localPath(absolute);
  if (!resolved) return undefined;
  // Uncaptured application source may belong to another context or have changed.
  if (/\.[cm]?[jt]sx?$/.test(resolved) && !/\.d\.[cm]?ts$/.test(resolved) && !under(resolved, compilerRoot)) return undefined;
  try { return fs.statSync(resolved).size <= 8 * 1024 * 1024 ? fs.readFileSync(resolved, "utf8") : undefined; }
  catch { return undefined; }
}
const configHost = {
  useCaseSensitiveFileNames: true,
  readDirectory: () => [],
  fileExists: file => sourceTexts.has(path.resolve(file)) || !!localPath(file),
  readFile: read,
};
let options = {allowJs: true, checkJs: true, jsx: ts.JsxEmit.Preserve, target: ts.ScriptTarget.ESNext};
const configDiagnostics = [];
if (request.configFile) {
  const config = ts.readConfigFile(request.configFile, read);
  if (config.error) configDiagnostics.push(config.error);
  else {
    const parsed = ts.parseJsonConfigFileContent(config.config, configHost, path.dirname(request.configFile));
    options = {...options, ...parsed.options};
    configDiagnostics.push(...parsed.errors);
  }
}
Object.assign(options, {noEmit: true, incremental: false, composite: false, plugins: [], skipLibCheck: true});
const host = ts.createCompilerHost(options, true);
host.readFile = read;
host.fileExists = configHost.fileExists;
host.writeFile = () => { throw new Error("typescript.emit-forbidden"); };
host.getSourceFile = (file, languageVersion) => {
  const text = read(file);
  return text === undefined ? undefined : ts.createSourceFile(file, text, languageVersion, true);
};
const program = ts.createProgram({rootNames: [...sourceTexts.keys()].sort(), options, host});
const checker = program.getTypeChecker();
const provenance = {provider: "typescript-api", providerVersion: String(ts.version), contextDigest: request.contextDigest};
const nodes = new Map(), edges = new Map(), declarations = new Map();
const diagnostics = [...configDiagnostics];
const diagnosticsByFile = new Map();
for (const file of program.getSourceFiles()) {
  if (!sourceTexts.has(path.resolve(file.fileName))) continue;
  const rows = [...program.getSyntacticDiagnostics(file), ...program.getSemanticDiagnostics(file)];
  diagnostics.push(...rows);
  diagnosticsByFile.set(file.fileName, rows);
}
function relative(file) {
  if (sourceTexts.has(path.resolve(file.fileName))) return path.relative(root, file.fileName).split(path.sep).join("/");
  const repoRelative = path.relative(root, file.fileName).split(path.sep).join("/");
  return !repoRelative.startsWith("../") ? repoRelative : "typescript-lib/" + path.relative(compilerRoot, file.fileName).split(path.sep).join("/");
}
function anchor(node) {
  const file = node.getSourceFile(), start = file.getLineAndCharacterOfPosition(node.getStart(file)), end = file.getLineAndCharacterOfPosition(node.end);
  return {file: relative(file), line: start.line + 1, column: start.character + 1, endLine: end.line + 1, endColumn: end.character + 1, columnEncoding: "utf16"};
}
function isCallable(node) {
  return ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isConstructorDeclaration(node) || ts.isMethodSignature(node) || ts.isCallSignatureDeclaration(node);
}
function declaredName(node) {
  if (node.name) return node.name.getText();
  if (node.parent && ts.isVariableDeclaration(node.parent)) return node.parent.name.getText();
  if (ts.isConstructorDeclaration(node)) return "constructor";
  return "anonymous@" + node.getStart();
}
function declaration(node) {
  if (!node) return undefined;
  if (declarations.has(node)) return declarations.get(node);
  const file = node.getSourceFile();
  if (!file) return undefined;
  const name = declaredName(node), parts = [];
  for (let parent = node.parent; parent && !ts.isSourceFile(parent); parent = parent.parent) {
    if (isCallable(parent) || ts.isClassDeclaration(parent) || ts.isInterfaceDeclaration(parent)) parts.unshift(declaredName(parent));
  }
  const owner = parts.length ? relative(file) + "#" + parts.join(".") : "";
  const qualifiedName = relative(file) + "#" + [...parts, name].join(".");
  // A lexical owner can be another function; it does not make a callback a method.
  const method = ts.isMethodDeclaration(node) || ts.isMethodSignature(node) || ts.isConstructorDeclaration(node);
  const kind = method ? "method" : isCallable(node) ? "function" : "type";
  let signature = "";
  if (isCallable(node)) {
    const typed = checker.getSignatureFromDeclaration(node);
    if (typed) signature = checker.signatureToString(typed);
  }
  const id = stable("typescript-node", qualifiedName, signature, String(node.kind));
  const local = sourceTexts.has(path.resolve(file.fileName));
  nodes.set(id, {recordKind: "node", id, kind, language: "typescript", qualifiedName,
    buildUnitId: request.unitId, analysisContextId: request.analysisContextId,
    sourceAnchors: local ? [anchor(node)] : [], symbolId: id, analysisCompleteness: "partial",
    attributes: {name, owner, signature, compilerVersion: String(ts.version)}, provenance});
  declarations.set(node, id);
  return id;
}
const files = program.getSourceFiles().filter(file => sourceTexts.has(path.resolve(file.fileName))).sort((a,b) => a.fileName.localeCompare(b.fileName));
for (const file of files) {
  const pending = [file];
  while (pending.length) {
    const node = pending.pop();
    if (isCallable(node) || ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) declaration(node);
    ts.forEachChild(node, child => { pending.push(child); });
  }
}
for (const file of files) {
  const moduleId = stable("typescript-module-init", relative(file));
  nodes.set(moduleId, {recordKind: "node", id: moduleId, kind: "function", language: "typescript",
    qualifiedName: relative(file) + "#<module-init>", buildUnitId: request.unitId,
    analysisContextId: request.analysisContextId, sourceAnchors: [anchor(file)],
    symbolId: moduleId, analysisCompleteness: "partial",
    attributes: {name: "<module-init>", syntheticRole: "module-initializer"}, provenance});
  const pending = [[file, moduleId]];
  while (pending.length) {
    const [node, inheritedCaller] = pending.pop();
    const caller = isCallable(node) ? declaration(node) : inheritedCaller;
    if (ts.isCallExpression(node) && caller) {
      const signature = checker.getResolvedSignature(node);
      const target = signature && declaration(signature.declaration);
      const site = anchor(node);
      if (target) {
        const invalidCall = (diagnosticsByFile.get(file.fileName) || []).some(row => typeof row.start === "number" && node.getStart() <= row.start && row.start < node.end);
        const resolution = invalidCall ? "candidate" : "exact";
        const id = stable("typescript-call", caller, target, JSON.stringify(site));
        edges.set(id, {recordKind: "edge", id, from: caller, to: target, relation: "calls", resolution,
          declarationResolution: resolution, dispatchResolution: "not-analyzed", bindingKind: "declared-exact",
          analysisContextId: request.analysisContextId, analysisCompleteness: "partial", callSite: site,
          operation: ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : node.expression.getText(),
          evidence: ["TypeScript checker resolved signature declaration; runtime dispatch not analyzed"], provenance});
      } else diagnostics.push({code: "unresolved-call", file, start: node.getStart(), messageText: "No compiler signature declaration for this call", category: ts.DiagnosticCategory.Warning});
    }
    ts.forEachChild(node, child => { pending.push([child, caller]); });
  }
}
for (const row of [...nodes.values()].sort((a,b) => a.id.localeCompare(b.id))) process.stdout.write(JSON.stringify(row) + "\n");
for (const row of [...edges.values()].sort((a,b) => a.id.localeCompare(b.id))) process.stdout.write(JSON.stringify(row) + "\n");
for (const item of diagnostics.slice(0, 2000)) process.stdout.write(JSON.stringify({recordKind: "diagnostic",
  code: "typescript." + item.code, message: ts.flattenDiagnosticMessageText(item.messageText, " ").slice(0, 1000),
  severity: item.category === ts.DiagnosticCategory.Error ? "warning" : "info", provider: "typescript-api",
  buildUnitId: request.unitId, subject: item.file ? relative(item.file) : request.unitId}) + "\n");
if (diagnostics.length > 2000) process.stdout.write(JSON.stringify({recordKind: "diagnostic", code: "typescript.diagnostics-truncated",
  message: String(diagnostics.length - 2000) + " additional compiler diagnostics", severity: "warning", provider: "typescript-api", buildUnitId: request.unitId}) + "\n");
