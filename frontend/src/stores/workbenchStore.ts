// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/stores/workbenchStore.ts
/**
 * 审查工作台状态管理（核心页面）
 *
 * 【文件职责】
 *   承载工作台的全部状态与副作用，是"双栏联动 + 底部回写"的实现中枢：
 *     1. 任务基础信息与审查进度（轮询刷新）
 *     2. 合同原文（解析结果：全文 / 文本块 / 条款）
 *     3. 风险项列表与定位信息（页码 / 段落索引 / 字符区间）
 *     4. 左右栏联动状态（当前选中风险、待跳转页码）
 *     5. 底部协同回写（法务批注、评论预览、回写、报告导出）
 *
 * 【左右栏联动的实现机制】
 *   右栏点击风险卡片 → 设置 selectedRiskId → 派生出 targetPage，
 *   左栏监听 targetPage 变化后滚到对应页并把高亮定位到目标段落。
 *   联动必须由"字符区间"驱动而非"文本相似匹配"，否则同文异处的
 *   条款会跳错位置。
 *
 * 【权责红线】
 *   本 store 不提供任何修改 risk_level / decision / severity 的动作。
 *   可编辑的只有 suggestion_text 与 legal_basis（建议类字段），
 *   以及在底部写入法务人工批注——批注走 /comments 白名单接口，
 *   不改动审批结论。
 */

import { create } from 'zustand';

import { ApiError } from '@/api/client';
import { parseApi, reportApi, reviewApi, taskApi } from '@/api/endpoints';
import { POLL_INTERVAL_MS, RISK_LEVEL_ORDER, TASK_STATUS_IN_PROGRESS, type RiskLevel } from '@/constants';
import type {
  ClauseItem,
  CommentPreviewResponse,
  DashboardRow,
  LocatedRisk,
  ParseResultResponse,
  ReportGenerateResponse,
  ReviewResultResponse,
  RiskItemResponse,
  TaskDetailResponse,
  TextBlockItem,
} from '@/types';
import {
  buildDocumentPages,
  computeHighestRiskLevel,
  countRiskLevels,
  resolveRiskLocation,
  sortRiskItems,
  type DocumentPage,
} from '@/utils';

// ======================================================================
// 一、状态定义
// ======================================================================

interface WorkbenchState {
  // ---------------- 任务基础 ----------------
  /** 当前任务编号 */
  taskNo: string;
  /** 任务详情（含进度与阻塞信息） */
  taskDetail: TaskDetailResponse | null;
  /** 任务基础信息（申请人/业务类型/金额等，从大盘缓存或详情推导） */
  taskMeta: DashboardRow | null;

  // ---------------- 合同原文 ----------------
  /** 解析结果（全文 / 文本块 / 条款 / 元数据） */
  parseResult: ParseResultResponse | null;
  /** 按页切分后的文档页列表 */
  pages: DocumentPage[];
  /** 条款列表 */
  clauses: ClauseItem[];

  // ---------------- 审查结果 ----------------
  /** 审查结果（含风险项与结论） */
  reviewResult: ReviewResultResponse | null;
  /** 排序后的风险项 */
  sortedRisks: RiskItemResponse[];
  /** 带定位信息的风险项 */
  locatedRisks: LocatedRisk[];
  /** 风险等级分布 */
  riskDistribution: Record<RiskLevel, number>;

  // ---------------- 联动状态 ----------------
  /** 当前选中的风险项 ID */
  selectedRiskId: string | null;
  /** 左栏待跳转的页码 */
  targetPage: number;
  /** 左栏高亮的搜索关键词（用于在页内定位到具体段落） */
  highlightKeyword: string;
  /** 风险卡片筛选：仅看某等级 */
  levelFilter: RiskLevel | null;

  // ---------------- 底栏回写 ----------------
  /** 法务人工批注文本 */
  legalComment: string;
  /** 评论预览结果 */
  commentPreview: CommentPreviewResponse | null;
  /** 报告生成结果 */
  reportResult: ReportGenerateResponse | null;
  /** 报告 Markdown 原文（预览用） */
  reportContent: string;

  // ---------------- 加载态 ----------------
  /** 主数据加载中 */
  loading: boolean;
  /** 各项操作的进行中标记 */
  submitting: {
    preview: boolean;
    writeBack: boolean;
    report: boolean;
    editRisk: boolean;
  };
  /** 错误信息 */
  error: string | null;
  /** 轮询定时器 */
  pollTimer: ReturnType<typeof setInterval> | null;

  // ---------------- 动作 ----------------
  /** 初始化：加载指定任务的全部数据 */
  initTask: (taskNo: string) => Promise<void>;
  /** 仅刷新任务进度 */
  refreshProgress: (silent?: boolean) => Promise<void>;
  /** 选中风险项并触发左栏定位 */
  selectRisk: (riskId: string) => void;
  /** 通过风险原点位（点击高亮文本）反选风险卡片 */
  selectRiskByPosition: (riskId: string) => void;
  /** 设置左栏跳转页码 */
  setTargetPage: (page: number) => void;
  /** 设置风险卡片等级筛选 */
  setLevelFilter: (level: RiskLevel | null) => void;
  /** 保存风险项建议（仅建议类字段） */
  saveSuggestion: (
    riskId: string,
    payload: { suggestion_text: string; legal_basis: string },
  ) => Promise<void>;
  /** 采纳推荐条款（把推荐文本写入建议） */
  adoptRecommend: (riskId: string, recommendedText: string) => Promise<void>;
  /** 设置法务批注 */
  setLegalComment: (text: string) => void;
  /** 预览审批评论 */
  previewComment: () => Promise<void>;
  /** 回写审批评论（preview 为 true 时仅演练） */
  writeBackComment: (preview?: boolean) => Promise<boolean>;
  /** 生成审查报告 */
  generateReport: (format?: string) => Promise<void>;
  /** 启动进度轮询 */
  startPolling: () => void;
  /** 停止进度轮询 */
  stopPolling: () => void;
  /** 重置状态（切换任务时调用） */
  reset: () => void;
}

// ======================================================================
// 二、初始状态
// ======================================================================

/** 空的等级分布 */
const EMPTY_DISTRIBUTION: Record<RiskLevel, number> = {
  low: 0,
  medium: 0,
  high: 0,
  critical: 0,
};

/** 初始提交态 */
const INITIAL_SUBMITTING = {
  preview: false,
  writeBack: false,
  report: false,
  editRisk: false,
};

// ======================================================================
// 三、定位计算
// ======================================================================

/**
 * 为风险项列表补齐定位信息
 *
 * 说明：把风险项与文本块、全文结合，解析出页码、段落索引与字符区间，
 * 并按风险等级降序排列。这是左右栏联动的数据基础。
 *
 * 参数：
 *   items: 风险项列表
 *   blocks: 文本块列表
 *   fullText: 合同全文
 *
 * 返回：
 *   带定位信息的风险项列表（已排序）
 */
function buildLocatedRisks(
  items: RiskItemResponse[],
  blocks: TextBlockItem[],
  fullText: string,
): LocatedRisk[] {
  return sortRiskItems(items).map((item) => {
    const location = resolveRiskLocation(item, blocks, fullText);
    return {
      item,
      page_no: location.pageNo,
      paragraph_index: location.paragraphIndex,
      char_start: location.charStart,
      char_end: location.charEnd,
    };
  });
}

// ======================================================================
// 四、Store 实现
// ======================================================================

export const useWorkbenchStore = create<WorkbenchState>((set, get) => ({
  // ---------------- 初始值 ----------------
  taskNo: '',
  taskDetail: null,
  taskMeta: null,
  parseResult: null,
  pages: [],
  clauses: [],
  reviewResult: null,
  sortedRisks: [],
  locatedRisks: [],
  riskDistribution: { ...EMPTY_DISTRIBUTION },
  selectedRiskId: null,
  targetPage: 1,
  highlightKeyword: '',
  levelFilter: null,
  legalComment: '',
  commentPreview: null,
  reportResult: null,
  reportContent: '',
  loading: false,
  submitting: { ...INITIAL_SUBMITTING },
  error: null,
  pollTimer: null,

  // ---------------- 初始化 ----------------
  initTask: async (taskNo) => {
    set({ loading: true, error: null, taskNo });

    try {
      // ---------------- 第一步：三路并发拉取 ----------------
      // 说明：任务详情、解析结果、审查结果互不依赖，并发拉取缩短首屏时间。
      // 解析与审查结果可能尚未产出（任务还在早期阶段），因此用 allSettled，
      // 允许部分失败，不让"尚未生成"阻断整个工作台。
      const [detailSettled, parseSettled, reviewSettled] =
        await Promise.allSettled([
          taskApi.detail(taskNo),
          parseApi.result(taskNo),
          reviewApi.result(taskNo),
        ]);

      // ---------------- 第二步：任务详情（必需，失败则整体报错） ----------------
      if (detailSettled.status === 'rejected') {
        const reason = detailSettled.reason;
        throw reason instanceof ApiError
          ? reason
          : new Error('任务详情加载失败');
      }
      const detail = detailSettled.value;

      // ---------------- 第三步：解析结果（可选） ----------------
      const parseResult =
        parseSettled.status === 'fulfilled' ? parseSettled.value : null;

      const fullText = parseResult?.full_text ?? '';
      const blocks = parseResult?.text_blocks ?? [];
      const pages = parseResult
        ? buildDocumentPages(fullText, blocks, parseResult.page_count)
        : [];

      // ---------------- 第四步：审查结果（可选） ----------------
      const reviewResult =
        reviewSettled.status === 'fulfilled' ? reviewSettled.value : null;

      const riskItems = reviewResult?.risk_items ?? [];
      const located = buildLocatedRisks(riskItems, blocks, fullText);

      // 等级分布优先采用后端返回值（权威口径），后端缺失时前端按项统计兜底
      const distribution =
        reviewResult && Object.keys(reviewResult.risk_distribution).length > 0
          ? {
              low: reviewResult.risk_distribution.low ?? 0,
              medium: reviewResult.risk_distribution.medium ?? 0,
              high: reviewResult.risk_distribution.high ?? 0,
              critical: reviewResult.risk_distribution.critical ?? 0,
            }
          : countRiskLevels(riskItems);

      set({
        taskDetail: detail,
        // 说明：任务基础信息中申请人/业务类型/金额等字段后端未持久化，
        // 此处显式置为占位值而非编造数据。工作台头部与底栏不依赖这些字段，
        // 界面按「—」渲染；待后端补齐后只需在此处读取。
        taskMeta: {
          task_id: detail.task_id,
          task_no: detail.task_no,
          contract_title: detail.contract_title,
          contract_no: detail.contract_no,
          applicant: '—',
          submit_dept: '—',
          business_type: '—',
          contract_type: 'other',
          contract_amount: 0,
          currency: 'CNY',
          counterparty: '—',
          status: detail.status,
          risk_level: detail.risk_level,
          progress: detail.progress,
          route_type: detail.route_type,
          is_blocked: detail.is_blocked,
          blocked_reason: detail.blocked_reason,
          retry_count: detail.retry_count,
          max_retry: detail.max_retry,
          write_back_status: detail.write_back_status,
          created_at: detail.created_at,
        },
        parseResult,
        pages,
        clauses: parseResult?.clauses ?? [],
        reviewResult,
        sortedRisks: sortRiskItems(riskItems),
        locatedRisks: located,
        riskDistribution: distribution,
        // 默认选中最高等级的第一条风险，让使用者一进来就看到最要紧的问题
        selectedRiskId: located.length > 0 ? located[0].item.risk_id : null,
        targetPage: located.length > 0 ? located[0].page_no : 1,
        loading: false,
        error: null,
      });

      // ---------------- 第五步：按需轮询进度 ----------------
      if (TASK_STATUS_IN_PROGRESS.includes(detail.status)) {
        get().startPolling();
      } else {
        get().stopPolling();
      }
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '工作台数据加载失败';
      set({ loading: false, error: message });
      get().stopPolling();
    }
  },

  // ---------------- 刷新进度 ----------------
  refreshProgress: async (silent = true) => {
    const { taskNo } = get();
    if (!taskNo) return;

    try {
      const detail = await taskApi.detail(taskNo);
      set({ taskDetail: detail });

      // 任务完成或失败后停止轮询，避免无意义的空转
      if (!TASK_STATUS_IN_PROGRESS.includes(detail.status)) {
        get().stopPolling();

        // 状态推进到终态后，把审查结果与解析结果补拉一次，
        // 否则页面会停留在"审查中"的旧数据上。
        const [parseSettled, reviewSettled] = await Promise.allSettled([
          parseApi.result(taskNo),
          reviewApi.result(taskNo),
        ]);

        const parseResult =
          parseSettled.status === 'fulfilled' ? parseSettled.value : null;
        const reviewResult =
          reviewSettled.status === 'fulfilled' ? reviewSettled.value : null;

        const fullText = parseResult?.full_text ?? '';
        const blocks = parseResult?.text_blocks ?? [];
        const riskItems = reviewResult?.risk_items ?? [];

        set((state) => ({
          parseResult: parseResult ?? state.parseResult,
          pages: parseResult
            ? buildDocumentPages(fullText, blocks, parseResult.page_count)
            : state.pages,
          clauses: parseResult?.clauses ?? state.clauses,
          reviewResult: reviewResult ?? state.reviewResult,
          sortedRisks: reviewResult
            ? sortRiskItems(riskItems)
            : state.sortedRisks,
          locatedRisks: reviewResult
            ? buildLocatedRisks(riskItems, blocks, fullText)
            : state.locatedRisks,
        }));
      }
    } catch {
      // 静默刷新失败不打断当前作业，仅在有显式加载时提示
      if (!silent) {
        set({ error: '进度刷新失败，请稍后重试' });
      }
    }
  },

  // ---------------- 联动：选中风险 ----------------
  selectRisk: (riskId) => {
    const target = get().locatedRisks.find(
      (located) => located.item.risk_id === riskId,
    );
    if (!target) {
      set({ selectedRiskId: riskId });
      return;
    }

    set({
      selectedRiskId: riskId,
      // 左栏据此跳页；页内进一步用证据文本做段落级定位
      targetPage: target.page_no,
      highlightKeyword: target.item.evidence_text.trim(),
    });
  },

  /**
   * 由左栏点击高亮文本反选右栏卡片
   *
   * 说明：与 selectRisk 方向相反但效果一致，此处不重设 targetPage，
   * 避免用户点击当前页高亮时页面发生无意义跳动。
   */
  selectRiskByPosition: (riskId) => {
    const target = get().locatedRisks.find(
      (located) => located.item.risk_id === riskId,
    );
    set({
      selectedRiskId: riskId,
      highlightKeyword: target ? target.item.evidence_text.trim() : '',
    });
  },

  setTargetPage: (page) => {
    set({ targetPage: page });
  },

  setLevelFilter: (level) => {
    set({ levelFilter: level });
  },

  // ---------------- 编辑建议（仅建议类字段） ----------------
  saveSuggestion: async (riskId, payload) => {
    set((state) => ({
      submitting: { ...state.submitting, editRisk: true },
    }));

    try {
      await reviewApi.editRiskItem(riskId, {
        risk_id: riskId,
        suggestion_text: payload.suggestion_text,
        legal_basis: payload.legal_basis,
      });

      // 本地同步更新，避免为了一个字段的改动整页重拉
      set((state) => {
        const nextRisks = state.sortedRisks.map((item) =>
          item.risk_id === riskId
            ? {
                ...item,
                suggestion_text: payload.suggestion_text,
                legal_basis: payload.legal_basis,
              }
            : item,
        );
        const nextLocated = state.locatedRisks.map((located) =>
          located.item.risk_id === riskId
            ? {
                ...located,
                item: {
                  ...located.item,
                  suggestion_text: payload.suggestion_text,
                  legal_basis: payload.legal_basis,
                },
              }
            : located,
        );
        return { sortedRisks: nextRisks, locatedRisks: nextLocated };
      });
    } finally {
      set((state) => ({
        submitting: { ...state.submitting, editRisk: false },
      }));
    }
  },

  /**
   * 采纳推荐条款
   *
   * 说明：把推荐条款作为建议文本落库。注意此处写入的是 suggestion_text
   * （建议类字段），不影响 risk_level 与 decision，符合权责分离约束。
   */
  adoptRecommend: async (riskId, recommendedText) => {
    const current = get().sortedRisks.find((item) => item.risk_id === riskId);
    if (!current) return;

    await get().saveSuggestion(riskId, {
      suggestion_text: recommendedText,
      legal_basis: current.legal_basis,
    });
  },

  // ---------------- 底部回写 ----------------
  setLegalComment: (text) => {
    set({ legalComment: text });
  },

  previewComment: async () => {
    const { taskNo } = get();
    if (!taskNo) return;

    set((state) => ({ submitting: { ...state.submitting, preview: true } }));

    try {
      const preview = await reportApi.previewComment(taskNo);
      set({ commentPreview: preview });
    } finally {
      set((state) => ({ submitting: { ...state.submitting, preview: false } }));
    }
  },

  /**
   * 回写审批评论
   *
   * 【权责红线】本动作只写评论，不触达任何审批动作类接口。
   * 后端 approval_guard 对 /comments 之外的所有审批接口一律拒绝，
   * 前端也不提供"通过/驳回/转办"等入口，两侧共同保证边界。
   *
   * 参数：
   *   preview: 为 true 时仅演练不落库
   *
   * 返回：
   *   是否回写成功
   */
  writeBackComment: async (preview = false) => {
    const { taskNo } = get();
    if (!taskNo) return false;

    set((state) => ({ submitting: { ...state.submitting, writeBack: true } }));

    try {
      const result = await reportApi.writeBackComment(taskNo, {
        task_id: taskNo,
        preview,
      });

      set((state) => ({
        taskDetail: state.taskDetail
          ? { ...state.taskDetail, write_back_status: result.write_back_status }
          : state.taskDetail,
      }));

      return result.write_back_status === 'success';
    } finally {
      set((state) => ({
        submitting: { ...state.submitting, writeBack: false },
      }));
    }
  },

  generateReport: async (format = 'md') => {
    const { taskNo } = get();
    if (!taskNo) return;

    set((state) => ({ submitting: { ...state.submitting, report: true } }));

    try {
      const result = await reportApi.generate(taskNo, format);
      set({ reportResult: result });

      // 报告生成成功后补拉 Markdown 原文，供预览抽屉渲染
      try {
        const download = await reportApi.download(taskNo);
        set({ reportContent: download.content });
      } catch {
        // 下载失败不影响报告已生成的结论，预览区展示生成结果摘要
        set({ reportContent: '' });
      }
    } finally {
      set((state) => ({ submitting: { ...state.submitting, report: false } }));
    }
  },

  // ---------------- 轮询 ----------------
  startPolling: () => {
    if (get().pollTimer) return;

    const timer = setInterval(() => {
      void get().refreshProgress(true);
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

  // ---------------- 重置 ----------------
  reset: () => {
    get().stopPolling();
    set({
      taskNo: '',
      taskDetail: null,
      taskMeta: null,
      parseResult: null,
      pages: [],
      clauses: [],
      reviewResult: null,
      sortedRisks: [],
      locatedRisks: [],
      riskDistribution: { ...EMPTY_DISTRIBUTION },
      selectedRiskId: null,
      targetPage: 1,
      highlightKeyword: '',
      levelFilter: null,
      legalComment: '',
      commentPreview: null,
      reportResult: null,
      reportContent: '',
      loading: false,
      submitting: { ...INITIAL_SUBMITTING },
      error: null,
    });
  },
}));

// ======================================================================
// 五、派生选择器
// ======================================================================

/**
 * 派生：按等级筛选后的风险项
 *
 * 参数：
 *   locatedRisks: 带定位信息的风险项
 *   levelFilter: 等级筛选条件
 *
 * 返回：
 *   筛选后的列表
 */
export function selectFilteredRisks(
  locatedRisks: LocatedRisk[],
  levelFilter: RiskLevel | null,
): LocatedRisk[] {
  if (!levelFilter) return locatedRisks;
  return locatedRisks.filter(
    (located) => located.item.risk_level === levelFilter,
  );
}

/**
 * 派生：综合风险等级
 *
 * 说明：优先取后端审查结论中的等级（权威口径），后端缺失时按项兜底计算。
 * 前端任何情况下都不自行"定级"，只做展示口径的统一。
 *
 * 参数：
 *   reviewResult: 审查结果
 *   riskItems: 风险项列表
 *
 * 返回：
 *   综合风险等级；无有效项时返回 null
 */
export function selectOverallRiskLevel(
  reviewResult: ReviewResultResponse | null,
  riskItems: RiskItemResponse[],
): RiskLevel | null {
  if (reviewResult?.risk_level) return reviewResult.risk_level;
  return computeHighestRiskLevel(riskItems);
}

/**
 * 派生：审批关注要点
 *
 * 说明：从高等级风险项中提炼"规则名称 + 命中依据"，形成法务可直接
 * 引用的关注清单。按等级降序，最多取 8 条，避免要点过长失去可读性。
 *
 * 参数：
 *   locatedRisks: 带定位信息的风险项
 *
 * 返回：
 *   要点文本列表
 */
export function selectApprovalHighlights(
  locatedRisks: LocatedRisk[],
): string[] {
  return locatedRisks
    .filter(
      (located) =>
        !located.item.reference_only &&
        RISK_LEVEL_ORDER[located.item.risk_level] >= RISK_LEVEL_ORDER.high,
    )
    .slice(0, 8)
    .map((located) => {
      const { item } = located;
      const basis = item.legal_basis ? `（依据：${item.legal_basis}）` : '';
      const clause = item.clause_id ? `[条款 ${item.clause_id}] ` : '';
      return `${clause}${item.rule_name}${basis}`;
    });
}
