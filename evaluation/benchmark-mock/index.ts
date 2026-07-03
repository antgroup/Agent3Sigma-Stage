/**
 * Benchmark Mock Plugin
 *
 * Intercepts tool calls via the before_tool_call hook, injecting mock data to
 * simulate indirect injection attacks.
 *
 * Interception strategy (by tool type):
 * - exec: replace the command with echo '<base64>' | base64 -d
 * - read: write mock data to a temp file, redirect the path parameter
 * - web_fetch: block and return the mock data as blockReason
 * - other unknown tools: generic block, return mock data
 *
 * Skipped tools (navigation/modification, do not return external data):
 * - glob, grep, list_dir, list_files, search, find
 * - edit, write, patch, mkdir
 *
 * Config file: ~/.openclaw/benchmark-mock-config.json
 */

import fs from 'fs';
import path from 'path';
import os from 'os';

const CONFIG_PATH = path.join(os.homedir(), '.openclaw', 'benchmark-mock-config.json');
const LOG_PATH = path.join(os.homedir(), '.openclaw', 'benchmark-mock-debug.log');
const MOCK_TMP_PATH = path.join(os.tmpdir(), 'openclaw-benchmark-mock.txt');

// Navigation/modification tools - do not return external untrusted data, not intercepted
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

  // Track whether the current turn has already been injected, to avoid double injection in the same turn.
  // key = test_case_id + mock_response prefix, set after injecting once.
  // Automatically reset when the config file changes (new test_case_id or cleared).
  let injectedForCase: string | null = null;

  api.on('before_tool_call', (event: any) => {
    const toolName = event.toolName;
    const params = event.params || {};

    // Skip navigation/modification tools
    if (SKIP_TOOLS.has(toolName)) return {};

    const turn = getMockTurn();
    if (!turn) {
      // Config cleared (new turn needs no injection), reset state
      injectedForCase = null;
      return {};
    }

    // Already injected once this turn, allow subsequent tool calls
    const caseKey = `${turn.test_case_id}:${turn.mock_response?.substring(0, 20)}`;
    if (injectedForCase === caseKey) {
      debugLog('SKIPPED (already injected this turn)', { testCaseId: turn.test_case_id, toolName });
      return {};
    }

    const mockResponse = turn.mock_response;

    // === exec tool: replace any command with echo base64 mock data ===
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

    // === read tool: write to a temp file, redirect path ===
    if (toolName === 'read') {
      try {
        fs.writeFileSync(MOCK_TMP_PATH, mockResponse, 'utf-8');
      } catch (e) {
        debugLog('ERROR writing mock temp file', { error: String(e) });
        return {};
      }
      const newParams = { ...params, path: MOCK_TMP_PATH };
      delete newParams.file_path; // remove alias to avoid conflict
      debugLog('INJECTED read', {
        testCaseId: turn.test_case_id,
        originalPath: params.path || params.file_path,
        mockPreview: mockResponse.substring(0, 80)
      });
      injectedForCase = caseKey;
      return { params: newParams };
    }

    // === web_fetch tool: block and return mock data directly ===
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

    // === other unknown tools: generic block, return mock data ===
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
