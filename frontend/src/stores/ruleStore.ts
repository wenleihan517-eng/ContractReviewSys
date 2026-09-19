// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/stores/ruleStore.ts
/**
 * 规则与模板状态管理（合规规则与模板页）
 *
 * 【文件职责】
 *   承载规则资产与示范条款库的全部状态与副作用：
 *     1. 规则列表（按合同类型分组构建树状结构）
 *     2. 规则的增删改与启用/禁用切换
 *     3. 示范条款库的查询与新增
 *
 * 【为什么在前端构建树】
 *   后端 /rules 接口返回扁平列表，按合同类型分组属于展示层关注点。
 *   在 store 中做一次分组并缓存，组件只消费树结构，
 *   避免每次渲染都重新分组（规则数量增长后这是明显的性能损耗）。
 *
 * 【权责提示】
 *   规则是判定逻辑的来源，规则的启用/禁用会直接改变审查结论。
 *   因此所有写操作都返回明确结果并触发列表重载，不做乐观更新，
 *   确保界面展示的规则状态与后端严格一致。
 */

import { create } from 'zustand';

import { ApiError } from '@/api/client';
import { ruleApi } from '@/api/endpoints';
import { CONTRACT_TYPE, CONTRACT_TYPE_LABEL } from '@/constants';
import type {
  RuleItemResponse,
  RuleTreeNode,
  RuleUpsertRequest,
  StandardClauseResponse,
  StandardClauseUpsertRequest,
} from '@/types';

// ======================================================================
// 一、状态定义
// ======================================================================

interface RuleState {
  // ---------------- 规则数据 ----------------
  /** 全量规则（扁平） */
  rules: RuleItemResponse[];
  /** 按合同类型分组的规则树 */
  ruleTree: RuleTreeNode[];
  /** 当前选中的规则编码 */
  selectedRuleCode: string | null;

  // ---------------- 示范条款 ----------------
  /** 示范条款库 */
  standardClauses: StandardClauseResponse[];

  // ---------------- 加载态 ----------------
  /** 规则加载中 */
  loadingRules: boolean;
  /** 条款加载中 */
  loadingClauses: boolean;
  /** 提交中标记（用于抽屉按钮 loading） */
  submitting: boolean;
  /** 错误信息 */
  error: string | null;

  // ---------------- 动作 ----------------
  /** 加载规则列表并构建树 */
  loadRules: (contractType?: string) => Promise<void>;
  /** 选中规则 */
  selectRule: (ruleCode: string | null) => void;
  /** 创建规则 */
  createRule: (payload: RuleUpsertRequest) => Promise<void>;
  /** 更新规则 */
  updateRule: (ruleCode: string, payload: RuleUpsertRequest) => Promise<void>;
  /** 启用/禁用规则 */
  setRuleEnabled: (ruleCode: string, enabled: boolean) => Promise<void>;
  /** 删除规则 */
  removeRule: (ruleCode: string) => Promise<void>;
  /** 加载示范条款库 */
  loadStandardClauses: (clauseType?: string) => Promise<void>;
  /** 新增示范条款 */
  createStandardClause: (
    payload: StandardClauseUpsertRequest,
  ) => Promise<void>;
}

// ======================================================================
// 二、树构建
// ======================================================================

/**
 * 分组展示顺序
 *
 * 说明：按"通用 → 采购 → 销售 → 服务 → 劳动"排序。
 * 通用规则放在最前，因为它的覆盖范围最广，是维护时的首要关注点。
 */
const GROUP_ORDER = [
  CONTRACT_TYPE.ALL,
  CONTRACT_TYPE.PURCHASE,
  CONTRACT_TYPE.SALES,
  CONTRACT_TYPE.SERVICE,
  CONTRACT_TYPE.LABOR,
];

/**
 * 把扁平规则列表构建为按合同类型分组的树
 *
 * 说明：一条规则可归属多个合同类型（contract_types 为数组），
 * 因此会同时出现在多个分组下。这不是数据重复，而是"按类型视角查看"
 * 的必然结果——法务按采购视角排查时，必须能看到跨类型的通用规则。
 * 同一条规则的编辑仍作用在同一个 rule_code 上，不会产生副本。
 *
 * 参数：
 *   rules: 扁平规则列表
 *
 * 返回：
 *   树节点列表，分组节点在前，分组内规则按严重度与优先级排序
 */
function buildRuleTree(rules: RuleItemResponse[]): RuleTreeNode[] {
  // ---------------- 第一步：按合同类型归集 ----------------
  const grouped = new Map<string, RuleItemResponse[]>();

  rules.forEach((rule) => {
    const types =
      rule.contract_types && rule.contract_types.length > 0
        ? rule.contract_types
        : [CONTRACT_TYPE.ALL];

    types.forEach((type) => {
      const list = grouped.get(type);
      if (list) {
        list.push(rule);
      } else {
        grouped.set(type, [rule]);
      }
    });
  });

  // ---------------- 第二步：按固定顺序输出分组 ----------------
  const tree: RuleTreeNode[] = [];

  GROUP_ORDER.forEach((type) => {
    const groupRules = grouped.get(type);
    if (!groupRules || groupRules.length === 0) return;

    // 分组内排序：严重度降序 → 优先级降序 → 编码升序
    const severityOrder: Record<string, number> = {
      fatal: 4,
      major: 3,
      minor: 2,
      info: 1,
    };
    const sorted = [...groupRules].sort((a, b) => {
      const severityDiff =
        (severityOrder[b.severity] ?? 0) - (severityOrder[a.severity] ?? 0);
      if (severityDiff !== 0) return severityDiff;
      const priorityDiff = (b.priority ?? 0) - (a.priority ?? 0);
      if (priorityDiff !== 0) return priorityDiff;
      return a.rule_code.localeCompare(b.rule_code);
    });

    tree.push({
      key: `group-${type}`,
      title: CONTRACT_TYPE_LABEL[type] ?? type,
      nodeType: 'group',
      ruleCount: sorted.length,
      enabledCount: sorted.filter((rule) => rule.enabled).length,
      children: sorted.map((rule) => ({
        key: `rule-${rule.rule_code}`,
        title: rule.rule_name,
        nodeType: 'rule' as const,
        rule,
      })),
    });
  });

  // ---------------- 第三步：兜底输出未覆盖的类型 ----------------
  // 说明：后端若新增了前端未预置的类型，不能被静默丢弃，
  // 否则法务会以为规则不存在。
  grouped.forEach((groupRules, type) => {
    if (GROUP_ORDER.includes(type as never)) return;

    tree.push({
      key: `group-${type}`,
      title: CONTRACT_TYPE_LABEL[type] ?? type,
      nodeType: 'group',
      ruleCount: groupRules.length,
      enabledCount: groupRules.filter((rule) => rule.enabled).length,
      children: groupRules.map((rule) => ({
        key: `rule-${rule.rule_code}`,
        title: rule.rule_name,
        nodeType: 'rule' as const,
        rule,
      })),
    });
  });

  return tree;
}

/**
 * 在树中查找指定编码的规则
 *
 * 参数：
 *   rules: 扁平规则列表
 *   ruleCode: 规则编码
 *
 * 返回：
 *   命中的规则；未命中返回 null
 */
function findRule(
  rules: RuleItemResponse[],
  ruleCode: string | null,
): RuleItemResponse | null {
  if (!ruleCode) return null;
  return rules.find((rule) => rule.rule_code === ruleCode) ?? null;
}

// ======================================================================
// 三、Store 实现
// ======================================================================

export const useRuleStore = create<RuleState>((set, get) => ({
  // ---------------- 初始值 ----------------
  rules: [],
  ruleTree: [],
  selectedRuleCode: null,
  standardClauses: [],
  loadingRules: false,
  loadingClauses: false,
  submitting: false,
  error: null,

  // ---------------- 加载规则 ----------------
  loadRules: async (contractType) => {
    set({ loadingRules: true, error: null });

    try {
      const result = await ruleApi.list(contractType);

      set({
        rules: result.items,
        ruleTree: buildRuleTree(result.items),
        loadingRules: false,
      });

      // 选中项失效时（被删除或不在当前筛选范围）清空选中，
      // 避免详情区展示一条已不存在的规则
      const { selectedRuleCode } = get();
      if (selectedRuleCode && !findRule(result.items, selectedRuleCode)) {
        set({ selectedRuleCode: null });
      }
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '规则列表加载失败';
      set({ loadingRules: false, error: message });
    }
  },

  selectRule: (ruleCode) => {
    set({ selectedRuleCode: ruleCode });
  },

  // ---------------- 创建规则 ----------------
  createRule: async (payload) => {
    set({ submitting: true });

    try {
      await ruleApi.create(payload);
      // 写操作后必须重载，保证界面与后端一致（不做乐观更新）
      await get().loadRules();
      set({ selectedRuleCode: payload.rule_code });
    } finally {
      set({ submitting: false });
    }
  },

  // ---------------- 更新规则 ----------------
  updateRule: async (ruleCode, payload) => {
    set({ submitting: true });

    try {
      await ruleApi.update(ruleCode, payload);
      await get().loadRules();
    } finally {
      set({ submitting: false });
    }
  },

  // ---------------- 启用/禁用 ----------------
  setRuleEnabled: async (ruleCode, enabled) => {
    set({ submitting: true });

    try {
      await ruleApi.setEnabled(ruleCode, enabled);

      // 说明：启用状态是本次操作的唯一变更点，用本地同步更新替代整表重载，
      // 避免树结构重建导致展开状态与滚动位置丢失（法务在批量调整时尤其明显）。
      set((state) => {
        const nextRules = state.rules.map((rule) =>
          rule.rule_code === ruleCode ? { ...rule, enabled } : rule,
        );
        return {
          rules: nextRules,
          ruleTree: buildRuleTree(nextRules),
        };
      });
    } finally {
      set({ submitting: false });
    }
  },

  // ---------------- 删除规则 ----------------
  removeRule: async (ruleCode) => {
    set({ submitting: true });

    try {
      await ruleApi.remove(ruleCode);
      await get().loadRules();
      set({ selectedRuleCode: null });
    } finally {
      set({ submitting: false });
    }
  },

  // ---------------- 示范条款 ----------------
  loadStandardClauses: async (clauseType) => {
    set({ loadingClauses: true });

    try {
      const result = await ruleApi.listStandardClauses(clauseType);
      set({ standardClauses: result.items, loadingClauses: false });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '示范条款库加载失败';
      set({ loadingClauses: false, error: message });
    }
  },

  createStandardClause: async (payload) => {
    set({ submitting: true });

    try {
      await ruleApi.createStandardClause(payload);
      await get().loadStandardClauses();
    } finally {
      set({ submitting: false });
    }
  },
}));

// ======================================================================
// 四、派生选择器
// ======================================================================

/**
 * 派生：当前选中的规则
 *
 * 参数：
 *   rules: 扁平规则列表
 *   selectedRuleCode: 选中编码
 *
 * 返回：
 *   规则对象；未选中返回 null
 */
export function selectCurrentRule(
  rules: RuleItemResponse[],
  selectedRuleCode: string | null,
): RuleItemResponse | null {
  return findRule(rules, selectedRuleCode);
}

/**
 * 派生：规则资产统计
 *
 * 参数：
 *   rules: 扁平规则列表
 *
 * 返回：
 *   { total, enabled, disabled, fatal, major }
 */
export function selectRuleSummary(rules: RuleItemResponse[]): {
  total: number;
  enabled: number;
  disabled: number;
  fatal: number;
  major: number;
} {
  return {
    total: rules.length,
    enabled: rules.filter((rule) => rule.enabled).length,
    disabled: rules.filter((rule) => !rule.enabled).length,
    fatal: rules.filter((rule) => rule.severity === 'fatal').length,
    major: rules.filter((rule) => rule.severity === 'major').length,
  };
}
