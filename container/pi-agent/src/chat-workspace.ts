import { access, lstat, mkdir, readFile, realpath, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, type BashOperations, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { AgentFailure, object } from "./protocol.js";
import { assertPublicText, callProjectTool, type ProjectToolsCapability } from "./project-tools.js";

export const CHAT_FILE_TOOLS = ["read", "write", "edit", "bash"];

export function createChatBashOperations(binding: ProjectToolsCapability, toolCallId: () => string): BashOperations {
  return {
    async exec(command, cwd, options) {
      const result = await callProjectTool(binding, toolCallId(), "_workspace_bash", {
        command, cwd, timeout_seconds: options.timeout ?? 120,
      }, options.signal);
      if (object(result.error) && typeof result.error.code === "string" && typeof result.error.message === "string") {
        throw new AgentFailure(result.error.code, result.error.message);
      }
      if (typeof result.output !== "string" || !Number.isInteger(result.exit_code)) {
        throw new AgentFailure("workspace_execution_invalid", "工作区命令未返回有效结果");
      }
      options.onData(Buffer.from(result.output));
      return { exitCode: result.exit_code as number };
    },
  };
}

export function createChatFileTools(cwd: string, binding: ProjectToolsCapability, ensureActive: () => void): ToolDefinition[] {
  const checked = async (value: string) => {
    ensureActive();
    const root = await realpath(cwd);
    const path = resolve(cwd, value);
    const suffix = relative(root, path);
    if (isAbsolute(suffix) || suffix === ".." || suffix.startsWith("../")) throw new AgentFailure("workspace_path_invalid", "文件必须位于当前会话工作区");
    // 在SDK完成~、@和Unicode路径展开后检查真实操作路径，连同悬空软链接一起拒绝。
    let existing = path;
    while (existing !== root) {
      try {
        if ((await lstat(existing)).isSymbolicLink()) throw new AgentFailure("workspace_path_invalid", "工作区文件路径不能包含软链接");
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      existing = dirname(existing);
    }
    return path;
  };
  const operations = {
    access: async (path: string) => access(await checked(path)),
    readFile: async (path: string) => readFile(await checked(path)),
    writeFile: async (path: string, content: string) => writeFile(await checked(path), content),
    mkdir: async (path: string) => { await mkdir(await checked(path), { recursive: true }); },
  };
  const definitions = [
    createReadToolDefinition(cwd, { operations }),
    createWriteToolDefinition(cwd, { operations }),
    createEditToolDefinition(cwd, { operations }),
  ];
  definitions[0]!.description = "读取当前会话工作区的UTF-8文本文件，可指定offset与limit按行读取。二进制附件请使用工作区已有命令工具解析；不能直接作为图片理解。";
  return definitions.map((definition) => {
    const tool = definition as ToolDefinition;
    const execute = tool.execute;
    return {
      ...tool,
      async execute(id, args, signal, onUpdate, context) {
        ensureActive();
        const result = await execute(id, args, signal, onUpdate, context);
        assertPublicText(JSON.stringify(result), binding.forbidden_literals);
        return result;
      },
    };
  });
}
