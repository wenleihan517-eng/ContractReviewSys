// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/stores/taskStore.ts
/**
 * 任务状态管理（合同审查大盘页）
 *
 * 【文件职责】
 *   维护大盘页的全部状态与副作用：
 *     1. 待审合同列表（含跨接口聚合出的完整行数据）
 *     2. 多条件筛选（风险等级 / 任务状态 / 合同类型 / 关键字）
 *     3. 本地分页与派生统计
 *     4. 手动上传、批量操作、阻塞任务重试
 *     5. 长任务实时进度轮询
 *
 * 【为什么在前端做聚合】
 *   后端任务列表接口只返回任务基础字段，不含申请人/业务类型/金额/风险等级。
 *   这些字段分散在任务详情与审查结果两个接口中。前端在列表加载后并发拉取
 *   详情补齐，既避免改动后端契约，也保证数据与后端同源。
 *
 * 【轮询设计】
 *   只要有任一任务处于进行中状态（pending/running/llm_done/rule_done），
 *   就启动定时轮询刷新列表，全部结束后自动停止，避免无意义的空转请求。
 */

import { create } from 'zustand';

import { taskApi } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import {
  CONTRACT_TYPE,
  POLL_INTERVAL_MS,
  RISK_LEVEL_ORDER,
  TASK_STATUS,
  TASK_STATUS_IN_PROGRESS,
  type RiskLevel,
  type TaskStatus,
} from '@/constants';
import type { DashboardRow, TaskDetailResponse } from '@/types';

// 任务详情缓存：轮询时复用，避免每个轮询周期对已完成任务重复打详情接口（N+1 节流，见 #13）
const detailCache = new Map<string, TaskDetailResponse>();

// ======================================================================
// 一、筛选条件模型
// ======================================================================

/** 大盘筛选条件 */
export interface DashboardFilters {
  /** 关键字（合同名称 / 编号 / 申请人） */
  keyword: string;
  /** 任务状态过滤 */
  status: TaskStatus | null;
  /** 风险等级过滤 */
  riskLevel: RiskLevel | null;
  /** 合同类型过滤 */
  contractType: string | null;
  /** 失败类型过滤（异常任务维度，2026-09-20 新增） */
  failureType: string | null;
}

/** 筛选条件初始值 */
const INITIAL_FILTERS: DashboardFilters = {
  keyword: '',
  status: null,
  riskLevel: null,
  contractType: null,
  failureType: null,
};

// ======================================================================
// 二、Store 状态与动作定义
// ======================================================================

interface TaskState {
  // ---------------- 数据 ----------------
  /** 聚合后的完整行数据 */
  rows: DashboardRow[];
  /** 加载中标记 */
  loading: boolean;
  /** 错误提示 */
  error: string | null;

  // ---------------- 筛选与分页 ----------------
  /** 筛选条件 */
  filters: DashboardFilters;
  /** 当前页码 */
  page: number;
  /** 每页条数 */
  pageSize: number;
  /** 服务端返回的匹配总数 */
  total: number;

  // ---------------- 选中与批量 ----------------
  /** 勾选的任务编号集合 */
  selectedTaskNos: string[];
  /** 行级操作中的任务编号（用于按钮 loading） */
  actioningTaskNos: string[];

  // ---------------- 轮询 ----------------
  /** 轮询定时器句柄 */
  pollTimer: ReturnType<typeof setInterval> | null;

  // ---------------- 动作 ----------------
  /** 更新筛选条件并重置到第一页重新加载 */
  setFilters: (patch: Partial<DashboardFilters>) => void;
  /** 重置全部筛选条件 */
  resetFilters: () => void;
  /** 切换分页 */
  setPage: (page: number, pageSize: number) => void;
  /** 加载任务列表（含详情聚合） */
  loadTasks: (options?: { silent?: boolean; polling?: boolean }) => Promise<void>;
  /** 勾选/取消勾选单个任务 */
  toggleSelect: (taskNo: string) => void;
  /** 全选/取消全选当前页 */
  setSelected: (taskNos: string[]) => void;
  /** 重试指定任务（失败或阻塞） */
  retryTask: (taskNo: string) => Promise<void>;
  /** 重新上传替换合同文件（仅提交经办人/管理员） */
  replaceFile: (
    taskNo: string,
    file: File,
  ) => Promise<{ task_no: string; accepted: boolean; message: string }>;
  /** 处理阻塞任务 */
  resolveBlockedTask: (
    taskNo: string,
    action: 'retry' | 'dismiss',
    reason: string,
  ) => Promise<void>;
  /** 批量重试失败/阻塞任务 */
  batchRetry: (taskNos: string[]) => Promise<{ succeeded: number; failed: number }>;
  /** 启动进度轮询 */
  startPolling: () => void;
  /** 停止进度轮询 */
  stopPolling: () => void;
}

// ======================================================================
// 三、聚合辅助函数
// ======================================================================

/**
 * 把任务列表项与详情合并为大盘行数据
 *
 * 【字段来源说明（重要）】
 *   已实测确认：后端 TaskListItem 与 TaskDetailResponse 均不返回
 *   申请人 / 送审部门 / 业务类型 / 合同金额 / 相对方 / 合同类型。
 *   这些字段在创建任务时提交给了 /tasks，但后端 ReviewTask 文档模型
 *   未持久化它们，因此读侧无数据源。
 *
 *   本函数的处理原则：**不编造数据**。
 *   无数据源的字段显式置为 '—' 与 0，界面据此展示占位符。
 *   在合规审查场景中，展示一个虚假的合同金额比展示「—」危险得多——
 *   使用者可能据此判断金额是否达到深度复核门槛。
 *
 *   待后端补齐字段持久化后，只需在此处读取即可，界面无需改动。
 *
 * 参数：
 *   listItem: 列表项基础数据
 *   detail: 任务详情（可能为 null，表示拉取失败）
 *
 * 返回：
 *   大盘行数据
 */
function mergeRow(
  listItem: {
    task_id: string;
    task_no: string;
    contract_title: string;
    status: TaskStatus;
    route_type: string;
    progress: number;
    blocked_reason: string | null;
    created_at: string | null;
    failure_type?: string | null;
    failure_reason?: string | null;
    created_by?: string;
  },
  detail: TaskDetailResponse | null,
): DashboardRow {
  return {
    task_id: listItem.task_id || detail?.task_id || '',
    task_no: listItem.task_no,
    contract_title:
      listItem.contract_title || detail?.contract_title || '未命名合同',
    contract_no: detail?.contract_no || '',
    // ---------------- 无后端数据源的字段 ----------------
    // 统一置为占位值，界面按「—」渲染，不编造数据
    applicant: '—',
    submit_dept: '—',
    business_type: '—',
    contract_amount: 0,
    currency: 'CNY',
    counterparty: '—',
    // ---------------- 有后端数据源的字段 ----------------
    // 合同类型：后端已持久化并回传；历史任务无此键时兜底为 all（前端筛选时归「通用」）
    contract_type: (detail?.contract_type as string) || CONTRACT_TYPE.ALL,
    status: listItem.status,
    risk_level: detail?.risk_level ?? null,
    progress: listItem.progress ?? detail?.progress ?? 0,
    route_type: listItem.route_type || detail?.route_type || '',
    is_blocked: detail?.is_blocked ?? listItem.status === TASK_STATUS.BLOCKED,
    blocked_reason: listItem.blocked_reason ?? detail?.blocked_reason ?? null,
    retry_count: detail?.retry_count ?? 0,
    max_retry: detail?.max_retry ?? 3,
    write_back_status: detail?.write_back_status ?? 'not_written',
    created_at: listItem.created_at ?? detail?.created_at ?? null,
    failure_type: listItem.failure_type ?? detail?.failure_type ?? null,
    failure_reason: listItem.failure_reason ?? detail?.failure_reason ?? null,
    created_by: listItem.created_by ?? '',
  };
}

/**
 * 对行数据应用前端侧筛选
 *
 * 说明：后端仅支持 status 与 keyword 过滤。风险等级与合同类型需前端过滤。
 * 为保证分页语义正确，当存在前端筛选条件时，先取全量再筛选再分页。
 *
 * 参数：
 *   rows: 全量行数据
 *   filters: 筛选条件
 *
 * 返回：
 *   过滤后的行数据
 */
function applyClientFilters(
  rows: DashboardRow[],
  filters: DashboardFilters,
): DashboardRow[] {
  return rows.filter((row) => {
    // 风险等级
    if (filters.riskLevel && row.risk_level !== filters.riskLevel) {
      return false;
    }
    // 合同类型
    if (filters.contractType && row.contract_type !== filters.contractType) {
      return false;
    }
    // 关键字（后端已过滤，此处兜底匹配更多字段）
    if (filters.keyword) {
      const kw = filters.keyword.trim().toLowerCase();
      const haystack = [
        row.contract_title,
        row.contract_no,
        row.applicant,
        row.counterparty,
      ]
        .join(' ')
        .toLowerCase();
      if (!haystack.includes(kw)) return false;
    }
    return true;
  });
}

// ======================================================================
// 四、Store 实现
// ======================================================================

export const useTaskStore = create<TaskState>((set, get) => ({
  // ---------------- 初始状态 ----------------
  rows: [],
  loading: false,
  error: null,
  filters: { ...INITIAL_FILTERS },
  page: 1,
  pageSize: 20,
  total: 0,
  selectedTaskNos: [],
  actioningTaskNos: [],
  pollTimer: null,

  // ---------------- 筛选 ----------------
  setFilters: (patch) => {
    set((state) => ({
      filters: { ...state.filters, ...patch },
      // 筛选条件变化后回到第一页，避免停留在越界页码上
      page: 1,
      selectedTaskNos: [],
    }));
    void get().loadTasks();
  },

  resetFilters: () => {
    set({
      filters: { ...INITIAL_FILTERS },
      page: 1,
      selectedTaskNos: [],
    });
    void get().loadTasks();
  },

  setPage: (page, pageSize) => {
    set({ page, pageSize });
    void get().loadTasks({ silent: true });
  },

  // ---------------- 加载列表 ----------------
  loadTasks: async (options) => {
    const { filters, page, pageSize } = get();
    const silent = options?.silent ?? false;

    if (!silent) {
      set({ loading: true, error: null });
    }

    try {
      // ---------------- 第一步：拉取任务列表 ----------------
      // 说明：存在前端侧筛选条件（风险等级/合同类型）时，需要先拿到更多数据
      // 才能正确筛选，因此把 page_size 放大到一页上限，在前端完成筛选与分页。
      const hasClientFilter = Boolean(
        filters.riskLevel || filters.contractType,
      );
      const fetchPage = hasClientFilter ? 1 : page;
      const fetchPageSize = hasClientFilter ? 200 : pageSize;

      const listResult = await taskApi.list({
        status: filters.status ?? undefined,
        page: fetchPage,
        page_size: fetchPageSize,
        keyword: filters.keyword || undefined,
        failure_type: filters.failureType ?? undefined,
      });

      if (listResult.items.length === 0) {
        set({ rows: [], total: 0, loading: false, error: null });
        get().stopPolling();
        return;
      }

      // ---------------- 第二步：并发拉取详情补齐字段 ----------------
      // 说明：用 allSettled 而非 all，单个详情失败不应导致整页数据丢失。
      const detailResults = await Promise.allSettled(
        listResult.items.map((item) => {
          const cached = detailCache.get(item.task_no);
          // 轮询场景节流：已缓存且风险等级已定、且当前非进行中的任务，
          // 直接复用缓存详情，避免每个轮询周期对已完成任务重复打详情接口（N+1）。
          if (
            options?.polling &&
            cached &&
            cached.risk_level != null &&
            !TASK_STATUS_IN_PROGRESS.includes(item.status)
          ) {
            return Promise.resolve(cached);
          }
          return taskApi.detail(item.task_no);
        }),
      );

      const mergedRows = listResult.items.map((item, index) => {
        const settled = detailResults[index];
        const detail =
          settled.status === 'fulfilled' ? settled.value : null;
        if (detail) detailCache.set(item.task_no, detail);
        return mergeRow(item, detail);
      });

      // ---------------- 第三步：前端筛选与分页 ----------------
      if (hasClientFilter) {
        const filtered = applyClientFilters(mergedRows, filters);
        const start = (page - 1) * pageSize;
        const paged = filtered.slice(start, start + pageSize);
        set({
          rows: paged,
          total: filtered.length,
          loading: false,
          error: null,
        });
      } else {
        set({
          rows: mergedRows,
          total: listResult.total,
          loading: false,
          error: null,
        });
      }

      // ---------------- 第四步：按需启动/停止轮询 ----------------
      const hasRunningTask = mergedRows.some((row) =>
        TASK_STATUS_IN_PROGRESS.includes(row.status),
      );
      if (hasRunningTask) {
        get().startPolling();
      } else {
        get().stopPolling();
      }
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '任务列表加载失败';
      set({ loading: false, error: message });
      get().stopPolling();
    }
  },

  // ---------------- 选择 ----------------
  toggleSelect: (taskNo) => {
    set((state) => {
      const exists = state.selectedTaskNos.includes(taskNo);
      return {
        selectedTaskNos: exists
          ? state.selectedTaskNos.filter((item) => item !== taskNo)
          : [...state.selectedTaskNos, taskNo],
      };
    });
  },

  setSelected: (taskNos) => {
    set({ selectedTaskNos: taskNos });
  },

  // ---------------- 单任务重试 ----------------
  retryTask: async (taskNo) => {
    set((state) => ({
      actioningTaskNos: [...state.actioningTaskNos, taskNo],
    }));

    try {
      await taskApi.retry(taskNo);
      // 重试后静默刷新，让进度条立刻反映新状态
      await get().loadTasks({ silent: true });
    } finally {
      set((state) => ({
        actioningTaskNos: state.actioningTaskNos.filter(
          (item) => item !== taskNo,
        ),
      }));
    }
  },

  // ---------------- 重新上传替换合同文件 ----------------
  replaceFile: async (taskNo, file) => {
    set((state) => ({
      actioningTaskNos: [...state.actioningTaskNos, taskNo],
    }));

    try {
      const result = await taskApi.replaceFile(taskNo, file);
      if (!result.accepted) {
        // 文件替换成功但重新解析未受理：抛出以提示，但不阻断刷新
        throw new ApiError(50007, result.message || '重新解析未受理', 200);
      }
      await get().loadTasks({ silent: true });
      return result;
    } finally {
      set((state) => ({
        actioningTaskNos: state.actioningTaskNos.filter(
          (item) => item !== taskNo,
        ),
      }));
    }
  },

  // ---------------- 处理阻塞任务 ----------------
  resolveBlockedTask: async (taskNo, action, reason) => {
    set((state) => ({
      actioningTaskNos: [...state.actioningTaskNos, taskNo],
    }));

    try {
      await taskApi.resolveBlocked(taskNo, {
        task_no: taskNo,
        action,
        reason,
      });
      await get().loadTasks({ silent: true });
    } finally {
      set((state) => ({
        actioningTaskNos: state.actioningTaskNos.filter(
          (item) => item !== taskNo,
        ),
      }));
    }
  },

  // ---------------- 批量重试 ----------------
  batchRetry: async (taskNos) => {
    let succeeded = 0;
    let failed = 0;

    // 说明：串行执行而非并发，避免瞬时打出大量请求压垮后端。
    for (const taskNo of taskNos) {
      try {
        // eslint-disable-next-line no-await-in-loop
        await taskApi.retry(taskNo);
        succeeded += 1;
      } catch {
        failed += 1;
      }
    }

    await get().loadTasks({ silent: true });
    return { succeeded, failed };
  },

  // ---------------- 轮询 ----------------
  startPolling: () => {
    const existing = get().pollTimer;
    if (existing) return; // 已在轮询，避免重复注册定时器

    const timer = setInterval(() => {
      void get().loadTasks({ silent: true, polling: true });
    }, POLL_INTERVAL_MS);

    set({ pollTimer: timer });
  },

  stopPolling: () => {
    const timer = get().pollTimer;
    if (timer) {
      clearInterval(timer);
      set({ pollTimer: null });
    }
  },
}));

// ======================================================================
// 五、派生选择器
// ======================================================================

/**
 * 派生：大盘统计摘要
 *
 * 说明：统计基于当前筛选后的行数据，反映"筛选视角下"的风险分布，
 * 与列表内容保持一致，避免出现"列表 3 条但统计显示 100 条"的错位。
 *
 * 参数：
 *   rows: 行数据
 *
 * 返回：
 *   { total, blocked, critical, high, completed }
 */
export function selectDashboardSummary(rows: DashboardRow[]): {
  total: number;
  blocked: number;
  critical: number;
  high: number;
  completed: number;
  failed: number;
} {
  return {
    total: rows.length,
    blocked: rows.filter((row) => row.status === TASK_STATUS.BLOCKED).length,
    critical: rows.filter((row) => row.risk_level === 'critical').length,
    high: rows.filter((row) => row.risk_level === 'high').length,
    completed: rows.filter((row) => row.status === TASK_STATUS.COMPLETED).length,
    failed: rows.filter((row) => row.status === TASK_STATUS.FAILED).length,
  };
}

/**
 * 派生：按风险等级降序排列的行数据
 *
 * 参数：
 *   rows: 行数据
 *
 * 返回：
 *   排序后的新数组
 */
export function selectRowsByRiskDesc(rows: DashboardRow[]): DashboardRow[] {
  return [...rows].sort((a, b) => {
    const aOrder = a.risk_level ? RISK_LEVEL_ORDER[a.risk_level] : 0;
    const bOrder = b.risk_level ? RISK_LEVEL_ORDER[b.risk_level] : 0;
    return bOrder - aOrder;
  });
}
