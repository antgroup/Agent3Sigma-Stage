/**
 * Benchmark Mock Plugin
 *
 * 通过 before_tool_call hook 拦截工具调用，注入 mock 数据模拟间接注入攻击。
 *
 * 拦截策略（按工具类型）:
 * - exec: 替换命令为 echo '<base64>' | base64 -d
 * - read: 将 mock 数据写入临时文件，重定向 path 参数
 * - web_fetch: 阻断并将 mock 数据作为 blockReason 返回
 * - 其他未知工具: 通用阻断，返回 mock 数据
 *
 * 跳过的工具（导航/修改类，不返回外部数据）:
 * - glob, grep, list_dir, list_files, search, find
 * - edit, write, patch, mkdir
 *
 * 配置文件: ~/.openclaw/benchmark-mock-config.json
 */

import fs from 'fs';
import path from 'path';
import os from 'os';

const CONFIG_PATH = path.join(os.homedir(), '.openclaw', 'benchmark-mock-config.json');
const LOG_PATH = path.join(os.homedir(), '.openclaw', 'benchmark-mock-debug.log');
const MOCK_TMP_PATH = path.join(os.tmpdir(), 'openclaw-benchmark-mock.txt');

// 导航/修改类工具 - 不返回外部不可信数据，不拦截
const SKIP_TOOLS = new Set([
  'glob', 'grep', 'list_dir', 'list_files',
  'search', 'find', 'edit', 'write', 'patch', 'mkdir',
]);

function debugLog(message: string, data?: any) {
  const timestamp = new Date().toISOString();
  const logLine = `[${timestamp}] ${message}${data ? ' ' + JSON.stringify(data) : ''}\n`;
  try {
    fs.appendFileSync(LOG_PATH, logLine);
  } catch {}
}

function loadMockConfig(): any {
  try {
    if (!fs.existsSync(CONFIG_PATH)) return null;
    return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  } catch {
    return null;
  }
}

function getMockTurn(): any {
  const config = loadMockConfig();
  if (!config?.turns?.length) return null;
  const turn = config.turns[0];
  if (!turn.injection_in_tool) return null;
  if (!turn.mock_response) return null;
  return { ...turn, test_case_id: config.test_case_id };
}

function register(api: any): void {
  debugLog('Plugin registered (once-per-turn injection mode)');

  // 记录当前轮次是否已注入，避免同一轮重复注入
  // key = test_case_id + mock_response 前缀，注入一次后设置
  // 当配置文件变更（新的 test_case_id 或被清除）时自动重置
  let injectedForCase: string | null = null;

  api.on('before_tool_call', (event: any) => {
    const toolName = event.toolName;
    const params = event.params || {};

    // 跳过导航/修改类工具
    if (SKIP_TOOLS.has(toolName)) return {};

    const turn = getMockTurn();
    if (!turn) {
      // 配置被清除（新轮次不需要注入），重置状态
      injectedForCase = null;
      return {};
    }

    // 本轮已经注入过一次，后续工具调用放行
    const caseKey = `${turn.test_case_id}:${turn.mock_response?.substring(0, 20)}`;
    if (injectedForCase === caseKey) {
      debugLog('SKIPPED (already injected this turn)', { testCaseId: turn.test_case_id, toolName });
      return {};
    }

    const mockResponse = turn.mock_response;

    // === exec 工具: 替换任意命令为 echo base64 mock 数据 ===
    if (toolName === 'exec' && params.command) {
      const encoded = Buffer.from(mockResponse, 'utf-8').toString('base64');
      debugLog('INJECTED exec', {
        testCaseId: turn.test_case_id,
        originalCommand: (params.command as string).substring(0, 100),
        mockPreview: mockResponse.substring(0, 80)
      });
      injectedForCase = caseKey;
      return {
        params: {
          ...params,
          command: `echo '${encoded}' | base64 -d`
        }
      };
    }

    // === read 工具: 写入临时文件，重定向 path ===
    if (toolName === 'read') {
      try {
        fs.writeFileSync(MOCK_TMP_PATH, mockResponse, 'utf-8');
      } catch (e) {
        debugLog('ERROR writing mock temp file', { error: String(e) });
        return {};
      }
      const newParams = { ...params, path: MOCK_TMP_PATH };
      delete newParams.file_path; // 移除别名，避免冲突
      debugLog('INJECTED read', {
        testCaseId: turn.test_case_id,
        originalPath: params.path || params.file_path,
        mockPreview: mockResponse.substring(0, 80)
      });
      injectedForCase = caseKey;
      return { params: newParams };
    }

    // === web_fetch 工具: 阻断并直接返回 mock 数据 ===
    if (toolName === 'web_fetch') {
      debugLog('INJECTED web_fetch', {
        testCaseId: turn.test_case_id,
        originalUrl: params.url,
        mockPreview: mockResponse.substring(0, 80)
      });
      injectedForCase = caseKey;
      return {
        block: true,
        blockReason: mockResponse
      };
    }

    // === 其他未知工具: 通用阻断，返回 mock 数据 ===
    debugLog('INJECTED (generic)', {
      testCaseId: turn.test_case_id,
      toolName,
      mockPreview: mockResponse.substring(0, 80)
    });
    injectedForCase = caseKey;
    return {
      block: true,
      blockReason: mockResponse
    };
  });
}

export default {
  id: 'benchmark-mock',
  name: 'Benchmark Mock',
  description: 'Intercept tool calls to inject mock data for security benchmark testing',
  register
};
