// SPDX-License-Identifier: Apache-2.0
// Real TypeScript SDK stdio calls, including schemas on isError responses.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, unlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { AjvJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/ajv";

const directory = path.dirname(fileURLToPath(import.meta.url));
const helper = path.resolve(directory, "../../tests/compat/drafting_typescript_server.py");
const { values } = parseArgs({ options: {
  python: { type: "string" },
  "output-dir": { type: "string" },
  report: { type: "string" },
} });
assert(values.python, "Supply --python /absolute/path/to/venv/bin/python");
// Resolve the venv executable path, but do not dereference its Python symlink.
const python = path.resolve(values.python);
assert(existsSync(python), "Python interpreter does not exist");
let outputDirectory;
if (values["output-dir"]) {
  outputDirectory = path.resolve(values["output-dir"]);
  mkdirSync(outputDirectory, { recursive: false });
} else {
  outputDirectory = mkdtempSync(path.join(tmpdir(), "veqtor-typescript-"));
}
outputDirectory = realpathSync(outputDirectory);
const reportPath = path.resolve(values.report ?? path.join(outputDirectory, "report.json"));
const environment = {
  ...process.env,
  VEQTOR_TRACKED_CHANGE_AUTHOR: "Shilov, Ilya",
  VEQTOR_COMMENT_INITIALS: "IS",
  VEQTOR_DISABLE_DECISION_RECORD: "1",
  VEQTOR_OPERATION_JOURNAL: "required",
};
delete environment.PYTHONPATH;
const fixtures = JSON.parse(execFileSync(python, ["-I", helper, "generate", outputDirectory], {
  encoding: "utf8", env: environment,
}));
const results = [];
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const text = (result) => result.content.filter((block) => block.type === "text")
  .map((block) => block.text).join("\n");
const payload = (result) => result.structuredContent ?? JSON.parse(text(result));
const draftingError = (result) => {
  assert(result.structuredContent, `save refusal omitted structured recovery: ${text(result)}`);
  return result.structuredContent;
};

async function scenario(name, fault, action) {
  const entry = { name, status: "failed", calls: [] };
  results.push(entry);
  const transport = new StdioClientTransport({
    command: python,
    args: fault ? ["-I", helper, "serve", fault] : ["-I", "-m", "veqtor_mcp.server"],
    env: environment,
    stderr: "pipe",
  });
  const client = new Client({ name: "veqtor-typescript-compat", version: "1.0.0" });
  let diagnostics = "";
  try {
    // Keep diagnostics out of protocol stdout, but retain them for failed runs.
    transport.stderr?.on("data", (chunk) => { diagnostics = (diagnostics + chunk).slice(-12000); });
    await client.connect(transport);
    const inventory = await client.listTools();
    const schemas = new Map(inventory.tools.map((tool) => [tool.name, tool.outputSchema]));
    for (const tool of ["inspect_document", "read_document_comments",
      "preflight_document_edits", "apply_document_edits", "recover_document_operation"]) {
      assert(schemas.get(tool), `${tool} must advertise an output schema`);
    }
    const provider = new AjvJsonSchemaValidator();
    const validators = new Map([...schemas].filter(([, schema]) => schema)
      .map(([name, schema]) => [name, provider.getValidator(schema)]));

    async function call(name, args, expectError = false) {
      const observed = { tool: name, expected_error: expectError };
      entry.calls.push(observed);
      // Use the SDK's actual callTool implementation; do not bypass validation
      // with a raw protocol request or retry an apply after a protocol error.
      const result = await client.callTool({ name, arguments: args });
      if (result.isError) observed.error_text = text(result);
      assert.equal(Boolean(result.isError), expectError, `${name}: unexpected error status`);
      if (result.structuredContent !== undefined) {
        const checked = validators.get(name)(result.structuredContent);
        assert(checked.valid, `${name}: structured output violates tools/list schema: ${checked.errorMessage}`);
        assert.deepEqual(JSON.parse(text(result)), result.structuredContent,
          `${name}: JSON text and structured content disagree`);
        observed.structured_schema_valid = true;
      } else {
        assert(expectError, `${name}: successful output must be structured`);
        observed.structured_schema_valid = null;
      }
      observed.returned_error = Boolean(result.isError);
      return result;
    }

    async function prepare(source, oldText = "Supply", newText = "Production") {
      const inspected = payload(await call("inspect_document", {
        path: source, mode: "browse", max_items: 1,
      }));
      assert.equal(inspected.file_sha256, sha(readFileSync(source)));
      const args = {
        source_path: source,
        file_sha256: inspected.file_sha256,
        operations: [{ kind: "replace_text", paragraph_ref: inspected.paragraphs[0].paragraph_ref,
          old_text: oldText, new_text: newText }],
      };
      const preflight = payload(await call("preflight_document_edits", args));
      assert.equal(preflight.batch_applicable, true);
      return { ...args, preflight_proof: preflight.preflight_proof };
    }
    await action({ call, prepare, entry });
    entry.status = "passed";
  } catch (error) {
    entry.error = String(error.stack ?? error);
    if (diagnostics) entry.server_diagnostics = diagnostics;
  } finally {
    await client.close().catch(() => {});
    await transport.close().catch(() => {});
  }
  console.log(`${entry.status.toUpperCase()}: ${name}`);
}

function verifyBackup(recovery, source, original, published) {
  assert.equal(recovery.output_path, source);
  assert.equal(recovery.backup_path_verified, true);
  assert.equal(recovery.publication_completed, published);
  assert.equal(recovery.backup_sha256, sha(original));
  assert.equal(path.dirname(recovery.backup_path), path.join(path.dirname(source), ".veqtor-backups"));
  assert.match(path.basename(recovery.backup_path), new RegExp(`\\.${sha(original).slice(0, 16)}\\.[a-f0-9]{32}\\.docx$`));
  assert.deepEqual(readFileSync(recovery.backup_path), original);
  assert.equal(!readFileSync(source).equals(original), published);
}

await scenario("successful save and own insertion re-edit", null, async ({ call, prepare, entry }) => {
  const source = fixtures.success;
  for (const [oldText, newText] of [["Supply", "Production"], ["Production", "Manufacturing"]]) {
    const before = readFileSync(source);
    const args = await prepare(source, oldText, newText);
    const applied = payload(await call("apply_document_edits", args));
    assert.equal(applied.record_status, "written");
    assert.equal(applied.record_id, args.preflight_proof.operation_id);
    assert.equal(applied.operation_journal.verification, "matched");
    const savedBytes = readFileSync(source);
    const recovered = payload(await call("recover_document_operation", {
      source_path: source, operation_id: applied.record_id,
    }));
    assert.equal(recovered.recovery_status, "existing_result");
    assert.deepEqual(readFileSync(source), savedBytes);
    assert.equal(applied.output_path, source);
    assert.equal(applied.save_mode, "in_place");
    assert.equal(applied.backup_sha256, sha(before));
    assert.deepEqual(readFileSync(applied.backup_path), before);
    assert.notEqual(sha(readFileSync(source)), sha(before));
    const read = payload(await call("inspect_document", { path: source, mode: "browse", max_items: 1 }));
    assert(read.paragraphs[0].text.includes(newText));
    entry.producer = applied.producer;
  }
  await call("read_document_comments", { path: source });
});

await scenario("Word owner file refusal", null, async ({ call, prepare, entry }) => {
  const source = fixtures.word_lock;
  const before = readFileSync(source);
  const args = await prepare(source);
  const owner = path.join(path.dirname(source), `~$${path.basename(source)}`);
  writeFileSync(owner, "synthetic Word owner", { flag: "wx" });
  try {
    const result = await call("apply_document_edits", args, true);
    const error = draftingError(result);
    assert.equal(error.schema_version, "drafting_error.v1");
    assert.equal(error.status, "error");
    assert.equal(error.error_code, "file_locked");
    assert.equal(error.recovery.output_path, source);
    assert.equal(error.recovery.publication_completed, false);
    assert.equal(error.recovery.backup_path, null);
    assert.equal(error.recovery.backup_sha256, null);
    assert.deepEqual(readFileSync(source), before);
    entry.error_code = error.error_code;
  } finally {
    unlinkSync(owner);
  }
});

for (const [fault, published, code] of [
  ["before_publish", false, "output_unwritable"],
  ["after_publish", true, "publication_uncertain"],
]) {
  await scenario(`late failure ${fault}`, fault, async ({ call, prepare, entry }) => {
    const source = fixtures[fault];
    const before = readFileSync(source);
    const args = await prepare(source);
    const result = await call("apply_document_edits", args, true);
    const error = draftingError(result);
    assert.equal(error.schema_version, "drafting_error.v1");
    assert.equal(error.status, "error");
    assert.equal(error.error_code, code);
    assert(!JSON.stringify(result).includes("PRIVATE_TYPESCRIPT"));
    verifyBackup(error.recovery, source, before, published);
    entry.error_code = code;
    entry.recovery = error.recovery;
  });
}

await scenario("published save with lost journal result recovers without reapply", "journal_finalize", async ({ call, prepare }) => {
  const source = fixtures.journal_finalize;
  const args = await prepare(source);
  assert.equal(args.preflight_proof.schema_version, "drafting_preflight.v2");
  const applied = payload(await call("apply_document_edits", args));
  assert.equal(applied.record_status, "write_failed");
  assert.equal(applied.operation_journal.publication_status, "published");
  assert.equal(applied.operation_journal.journal_status, "incomplete");
  assert(!JSON.stringify(applied).includes("PRIVATE_TYPESCRIPT_JOURNAL"));
  const saved = readFileSync(source);
  assert.equal(sha(saved), args.preflight_proof.candidate_sha256);
  const argsRecovery = { source_path: source, operation_id: args.preflight_proof.operation_id };
  const recovered = payload(await call("recover_document_operation", argsRecovery));
  assert.equal(recovered.record_status, "written");
  assert.equal(recovered.recovered, true);
  assert.equal(recovered.recovery_status, "recovered");
  const repeated = payload(await call("recover_document_operation", argsRecovery));
  assert.equal(repeated.recovery_status, "existing_result");
  assert.deepEqual(readFileSync(source), saved);
});

await scenario("invalid arguments and proof binding", null, async ({ call, prepare }) => {
  const source = fixtures.invalid;
  const before = readFileSync(source);
  const valid = await prepare(source);
  const invalid = await call("apply_document_edits", {
    ...valid, file_sha256: "PRIVATE_INVALID_INPUT",
  }, true);
  assert.match(text(invalid), /invalid_arguments/);
  assert(!text(invalid).includes("PRIVATE_INVALID_INPUT"));
  assert(!text(invalid).includes(source));
  const drift = structuredClone(valid);
  drift.operations[0].new_text = "PRIVATE_PROOF_DRIFT";
  const refused = await call("apply_document_edits", drift, true);
  assert.match(text(refused), /preflight_binding_mismatch/);
  assert(!text(refused).includes("PRIVATE_PROOF_DRIFT"));
  assert(!text(refused).includes(source));
  assert.deepEqual(readFileSync(source), before);
});

const report = {
  schema_version: "typescript_drafting_compat.v1",
  sdk: "@modelcontextprotocol/sdk@1.32.1",
  node: process.version,
  interpreter: python,
  output_directory: outputDirectory,
  passed: results.every((entry) => entry.status === "passed"),
  cases: results,
};
writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
console.log(`Report: ${reportPath}`);
if (!report.passed) process.exitCode = 1;
