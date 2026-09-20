// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/RulesPage.tsx
/**
 * 页面三：合规规则与模板
 *
 * 【页面职责】
 *   规则资产的维护台，承担三件事：
 *     1. 按合同类型（采购、销售、服务、劳动）分组管理审查规则
 *     2. 规则树状列表 + 新增/编辑/启用禁用规则
 *     3. 标准示范条款库维护
 *
 * 【树状列表的语义】
 *   一条规则可同时归属多个合同类型（例如"争议解决管辖地违规"对四类合同都适用）。
 *   因此同一条规则会出现在多个分组下——这是"按类型视角查看"的必然结果，
 *   不是数据重复。无论从哪个分组进入编辑，操作的都是同一个 rule_code。
 *   界面上明确标注"跨类型规则"，避免法务误以为需要改四处。
 *
 * 【为什么写操作不做乐观更新】
 *   规则的启用/禁用会直接改变审查判定结果。若前端先行更新而请求失败，
 *   法务会看到"已禁用"的假状态并据此判断，这在合规场景中风险极高。
 *   因此所有写操作都等待后端确认，切换开关时才反映真实状态。
 *
 * 【示范条款库的定位】
 *   这里是"标准条款"的权威来源，供法务在修改合同时直接取用。
 *   规则中的 suggestion_template 是自动生成建议的模板，
 *   两者职责不同：前者供人查阅引用，后者供引擎生成建议。
 */

import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Tree,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { DataNode } from 'antd/es/tree';
import {
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  FileProtectOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';

import {
  CLAUSE_TYPE_OPTIONS,
  CONTRACT_TYPE_COLOR,
  CONTRACT_TYPE_LABEL,
  MATCH_TYPE_LABEL,
  MATCH_TYPE_OPTIONS,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_OPTIONS,
  RULE_CONTRACT_TYPE_OPTIONS,
  RULE_SEVERITY_LABEL,
  RULE_SEVERITY_OPTIONS,
} from '@/constants';
import {
  selectRuleSummary,
  useRuleStore,
} from '@/stores/ruleStore';
import type {
  RuleItemResponse,
  RuleTreeNode,
  RuleUpsertRequest,
  StandardClauseResponse,
  StandardClauseUpsertRequest,
} from '@/types';
import { useResponsive } from '@/hooks/useResponsive';

const { Text, Paragraph, Title } = Typography;
const { TextArea } = Input;

/** 严重度对应的展示颜色（与后端 RuleSeverity → RiskLevel 映射口径一致） */
const SEVERITY_COLOR: Record<string, string> = {
  fatal: 'error',
  major: 'volcano',
  minor: 'warning',
  info: 'default',
};

/** 匹配类型说明，辅助法务理解规则如何生效 */
const MATCH_TYPE_HINT: Record<string, string> = {
  keyword: '命中任一关键词即触发',
  regex: '按正则表达式匹配原文',
  clause_type: '按条款类型存在性判断',
  semantic: '按语义相似度判断（向量召回的辅助判断）',
  field_compare: '按结构化字段比较',
  missing_clause: '合同中缺失该必备条款即触发',
  numeric_threshold: '按数值阈值判断',
};

export default function RulesPage(): React.ReactElement {
  const { message, modal } = App.useApp();
  const [ruleForm] = Form.useForm<RuleUpsertRequest>();
  const [clauseForm] = Form.useForm<StandardClauseUpsertRequest>();

  // ---------------- Store ----------------
  const rules = useRuleStore((state) => state.rules);
  const ruleTree = useRuleStore((state) => state.ruleTree);
  const selectedRuleCode = useRuleStore((state) => state.selectedRuleCode);
  const standardClauses = useRuleStore((state) => state.standardClauses);
  const loadingRules = useRuleStore((state) => state.loadingRules);
  const loadingClauses = useRuleStore((state) => state.loadingClauses);
  const submitting = useRuleStore((state) => state.submitting);
  const error = useRuleStore((state) => state.error);

  const loadRules = useRuleStore((state) => state.loadRules);
  const selectRule = useRuleStore((state) => state.selectRule);
  const createRule = useRuleStore((state) => state.createRule);
  const updateRule = useRuleStore((state) => state.updateRule);
  const setRuleEnabled = useRuleStore((state) => state.setRuleEnabled);
  const removeRule = useRuleStore((state) => state.removeRule);
  const loadStandardClauses = useRuleStore((state) => state.loadStandardClauses);
  const createStandardClause = useRuleStore(
    (state) => state.createStandardClause,
  );

  /** 当前视口是否处于手机尺寸（< md） */
  const { isMobile } = useResponsive();

  // ---------------- 本地 UI 状态 ----------------
  /** 规则编辑抽屉/弹窗模式：null 表示关闭 */
  const [ruleModalMode, setRuleModalMode] = useState<'create' | 'edit' | null>(
    null,
  );
  /** 条款新增弹窗 */
  const [clauseModalOpen, setClauseModalOpen] = useState(false);
  /** 树搜索关键字 */
  const [treeKeyword, setTreeKeyword] = useState('');
  /** 展开的分组 key */
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  /** 条款库条款类型筛选 */
  const [clauseTypeFilter, setClauseTypeFilter] = useState<string | undefined>();
  /**
   * 统计卡筛选维度（2026-09-20：点击统计卡切入对应筛选）
   * null=全部；enabled/disabled=启用状态；fatal/major=严重级别
   */
  const [statCardFilter, setStatCardFilter] = useState<
    'enabled' | 'disabled' | 'fatal' | 'major' | null
  >(null);

  // ---------------- 首屏加载 ----------------
  useEffect(() => {
    void loadRules();
    void loadStandardClauses();
  }, [loadRules, loadStandardClauses]);

  // ---------------- 默认展开全部分组 ----------------
  useEffect(() => {
    if (ruleTree.length > 0 && expandedKeys.length === 0) {
      setExpandedKeys(ruleTree.map((group) => group.key));
    }
  }, [ruleTree, expandedKeys.length]);

  // ====================================================================
  // 派生数据
  // ====================================================================

  /** 规则资产统计 */
  const summary = useMemo(() => selectRuleSummary(rules), [rules]);

  /** 当前选中的规则 */
  const currentRule = useMemo(
    () => rules.find((rule) => rule.rule_code === selectedRuleCode) ?? null,
    [rules, selectedRuleCode],
  );

  /**
   * 应用搜索过滤后的树
   *
   * 说明：搜索匹配规则名称、编码与描述。
   * 分组下若无匹配项则整组隐藏，避免留下空分组造成干扰。
   */
  const filteredTree = useMemo<RuleTreeNode[]>(() => {
    if (!treeKeyword.trim() && !statCardFilter) return ruleTree;

    const keyword = treeKeyword.trim().toLowerCase();

    return ruleTree
      .map((group) => {
        const matchedChildren = (group.children ?? []).filter((child) => {
          const rule = child.rule;
          if (!rule) return false;

          // 统计卡维度过滤（点击卡片切入，再点一次取消）
          if (statCardFilter === 'enabled' && !rule.enabled) return false;
          if (statCardFilter === 'disabled' && rule.enabled) return false;
          if (statCardFilter === 'fatal' && rule.severity !== 'fatal') {
            return false;
          }
          if (statCardFilter === 'major' && rule.severity !== 'major') {
            return false;
          }

          if (!keyword.trim()) return true;

          const haystack = [
            rule.rule_code,
            rule.rule_name,
            rule.description,
            rule.legal_basis,
          ]
            .join(' ')
            .toLowerCase();
          return haystack.includes(keyword);
        });

        return { ...group, children: matchedChildren };
      })
      .filter((group) => (group.children ?? []).length > 0);
  }, [ruleTree, treeKeyword, statCardFilter]);

  /**
   * 转换为 Ant Design Tree 数据
   *
   * 说明：分组节点展示"启用数 / 总数"，让法务一眼看出某类型下
   * 有多少规则处于生效状态——禁用规则过多是审查失效的常见隐患。
   */
  const treeData = useMemo<DataNode[]>(
    () =>
      filteredTree.map((group) => ({
        key: group.key,
        title: (
          <Space size={6}>
            <Tag
              color={CONTRACT_TYPE_COLOR[group.key.replace('group-', '')] ?? 'default'}
              style={{ margin: 0, fontSize: 11 }}
            >
              {group.title}
            </Tag>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {group.enabledCount ?? 0} / {group.ruleCount ?? 0} 启用
            </Text>
          </Space>
        ),
        children: (group.children ?? []).map((child) => {
          const rule = child.rule;
          return {
            key: child.key,
            title: (
              <Space size={6} wrap={false}>
                <Text
                  style={{
                    fontSize: 12,
                    // 禁用规则以灰色弱化，与启用规则形成清晰区分
                    color: rule?.enabled ? undefined : '#bfbfbf',
                  }}
                >
                  {child.title}
                </Text>
                {rule && (
                  <Tag
                    color={SEVERITY_COLOR[rule.severity] ?? 'default'}
                    style={{
                      margin: 0,
                      fontSize: 10,
                      lineHeight: '14px',
                    }}
                  >
                    {RULE_SEVERITY_LABEL[
                      rule.severity as keyof typeof RULE_SEVERITY_LABEL
                    ]?.split('（')[0] ?? rule.severity}
                  </Tag>
                )}
                {rule && rule.contract_types.length > 1 && (
                  <Tooltip title={`该规则同时适用于：${rule.contract_types.map((type) => CONTRACT_TYPE_LABEL[type] ?? type).join('、')}`}>
                    <Tag
                      color="default"
                      style={{ margin: 0, fontSize: 10, lineHeight: '14px' }}
                    >
                      跨类型
                    </Tag>
                  </Tooltip>
                )}
              </Space>
            ),
            isLeaf: true,
          };
        }),
      })),
    [filteredTree],
  );

  // ====================================================================
  // 交互处理
  // ====================================================================

  /** 打开新建规则弹窗 */
  const handleOpenCreate = (): void => {
    setRuleModalMode('create');
    ruleForm.resetFields();
    // 预置合理默认值，减少必填项的填写成本
    ruleForm.setFieldsValue({
      severity: 'major',
      match_type: 'keyword',
      enabled: true,
      priority: 50,
      contract_types: ['all'],
      match_config: { keywords: [] },
    });
  };

  /** 打开编辑规则弹窗 */
  const handleOpenEdit = (rule: RuleItemResponse): void => {
    setRuleModalMode('edit');
    ruleForm.setFieldsValue({
      rule_code: rule.rule_code,
      rule_name: rule.rule_name,
      description: rule.description,
      match_type: rule.match_type,
      // 说明：match_config 的后端类型为 Dict[str, Any]，
      // 前端类型为 Record<string, unknown>，与表单字段的索引签名不完全兼容，
      // 此处断言为纯对象类型，保证表单回填不丢字段。
      match_config: rule.match_config as Record<string, object | undefined>,
      contract_types: rule.contract_types,
      severity: rule.severity,
      override_risk_level: rule.override_risk_level,
      suggestion_template: rule.suggestion_template,
      legal_basis: rule.legal_basis,
      priority: rule.priority,
      enabled: rule.enabled,
    });
  };

  /**
   * 提交规则表单
   *
   * 说明：match_config 采用 JSON 文本输入框。
   * 规则配置的结构因 match_type 而异（keyword 要 keywords 数组、
   * regex 要 pattern 字符串、numeric_threshold 要 field/operator/value），
   * 用结构化表单需要七套动态字段，维护成本高且容易与后端新增类型脱节。
   * JSON 输入配合格式校验与示例提示，是更稳妥的选择。
   */
  const handleSubmitRule = (): void => {
    void (async () => {
      let values: RuleUpsertRequest;
      try {
        values = await ruleForm.validateFields();
      } catch {
        return;
      }

      try {
        if (ruleModalMode === 'create') {
          await createRule(values);
          message.success(`规则 ${values.rule_code} 已创建`);
        } else {
          await updateRule(values.rule_code, values);
          message.success(`规则 ${values.rule_code} 已更新`);
        }
        setRuleModalMode(null);
      } catch (submitError) {
        const detail =
          submitError instanceof Error ? submitError.message : '提交失败';
        message.error(`保存失败：${detail}`);
      }
    })();
  };

  /** 切换规则启用状态（带确认，因为会影响判定结果） */
  const handleToggleEnabled = (
    rule: RuleItemResponse,
    enabled: boolean,
  ): void => {
    const actionText = enabled ? '启用' : '禁用';
    const riskNote = enabled
      ? '启用后该规则将参与审查判定，命中即按规则严重度定级。'
      : '禁用后该规则不再参与判定，命中该风险将不再被识别。';

    modal.confirm({
      title: `确认${actionText}规则「${rule.rule_name}」？`,
      icon: <ExclamationCircleOutlined />,
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          <div>
            规则编码：<Text code>{rule.rule_code}</Text>
          </div>
          <div>
            适用类型：
            {rule.contract_types
              .map((type) => CONTRACT_TYPE_LABEL[type] ?? type)
              .join('、')}
          </div>
          <div style={{ marginTop: 6, color: '#8c8c8c' }}>{riskNote}</div>
        </div>
      ),
      okText: `确认${actionText}`,
      cancelText: '取消',
      okButtonProps: { danger: !enabled },
      onOk: async () => {
        try {
          await setRuleEnabled(rule.rule_code, enabled);
          message.success(`规则已${actionText}`);
        } catch {
          // 失败时不改变界面状态，保证展示与后端一致
          message.error(`${actionText}失败，请稍后重试`);
        }
      },
    });
  };

  /** 删除规则 */
  const handleDelete = (rule: RuleItemResponse): void => {
    modal.confirm({
      title: `确认删除规则「${rule.rule_name}」？`,
      icon: <ExclamationCircleOutlined />,
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          <div>
            规则编码：<Text code>{rule.rule_code}</Text>
          </div>
          <div style={{ color: '#cf1322', marginTop: 6 }}>
            删除后该规则的判定逻辑将彻底从规则库移除，历史审查记录不受影响。
            若只是暂时不需要，建议改为"禁用"以便后续恢复。
          </div>
        </div>
      ),
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await removeRule(rule.rule_code);
          message.success('规则已删除');
        } catch {
          message.error('删除失败，请稍后重试');
        }
      },
    });
  };

  /** 提交示范条款 */
  const handleSubmitClause = (): void => {
    void (async () => {
      let values: StandardClauseUpsertRequest;
      try {
        values = await clauseForm.validateFields();
      } catch {
        return;
      }

      try {
        await createStandardClause(values);
        message.success('示范条款已入库');
        setClauseModalOpen(false);
        clauseForm.resetFields();
      } catch (submitError) {
        const detail =
          submitError instanceof Error ? submitError.message : '提交失败';
        message.error(`保存失败：${detail}`);
      }
    })();
  };

  // ====================================================================
  // 示范条款表格列
  // ====================================================================

  const clauseColumns: ColumnsType<StandardClauseResponse> = [
    {
      title: '条款名称',
      dataIndex: 'clause_title',
      key: 'clause_title',
      width: 180,
      render: (value: string, row) => (
        <div>
          <Text strong style={{ fontSize: 13 }}>
            {value}
          </Text>
          <div style={{ fontSize: 11, color: '#8c8c8c' }}>{row.clause_id}</div>
        </div>
      ),
    },
    {
      title: '条款类型',
      dataIndex: 'clause_type',
      key: 'clause_type',
      width: 120,
      render: (value: string) => (
        <Tag color="default" style={{ margin: 0, fontSize: 11 }}>
          {CLAUSE_TYPE_OPTIONS.find((option) => option.value === value)?.label ??
            value ??
            '未分类'}
        </Tag>
      ),
    },
    {
      title: '适用合同类型',
      dataIndex: 'contract_types',
      key: 'contract_types',
      width: 180,
      render: (types: string[]) => (
        <Space size={4} wrap>
          {(types.length > 0 ? types : ['all']).map((type) => (
            <Tag
              key={type}
              color={CONTRACT_TYPE_COLOR[type] ?? 'default'}
              style={{ margin: 0, fontSize: 11 }}
            >
              {CONTRACT_TYPE_LABEL[type] ?? type}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '条款内容',
      dataIndex: 'clause_text',
      key: 'clause_text',
      render: (value: string) => (
        <Tooltip title={value.length > 120 ? '点击展开查看' : undefined}>
          <Paragraph
            style={{ margin: 0, fontSize: 12, lineHeight: 1.7, color: '#595959' }}
            ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
          >
            {value}
          </Paragraph>
        </Tooltip>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      align: 'center',
      render: (_value, row) => (
        <Tooltip title="复制条款全文，可直接粘贴至合同修订稿">
          <Button
            type="link"
            size="small"
            onClick={async () => {
              const { copyToClipboard } = await import('@/utils');
              const succeeded = await copyToClipboard(row.clause_text);
              if (succeeded) {
                message.success('条款已复制');
              } else {
                message.error('复制失败');
              }
            }}
          >
            复制
          </Button>
        </Tooltip>
      ),
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* ================= 顶部统计 ================= */}
      <Row gutter={12}>
        <Col xs={12} sm={12} md={5}>
          <Card
              size="small"
              hoverable
              onClick={() => setStatCardFilter(null)}
              style={{
                cursor: 'pointer',
                ...(statCardFilter === null
                  ? { borderColor: '#1677ff', borderWidth: 2 }
                  : {}),
              }}
            >
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>规则总数</Text>
              <div style={{ fontSize: 22, fontWeight: 600 }}>{summary.total}</div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={12} md={5}>
          <Card
              size="small"
              hoverable
              onClick={() =>
                setStatCardFilter(
                  statCardFilter === 'enabled' ? null : 'enabled',
                )
              }
              style={{
                cursor: 'pointer',
                ...(statCardFilter === 'enabled'
                  ? { borderColor: '#1677ff', borderWidth: 2 }
                  : {}),
              }}
            >
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>已启用</Text>
              <div style={{ fontSize: 22, fontWeight: 600, color: '#52c41a' }}>
                {summary.enabled}
              </div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={12} md={5}>
          <Card
              size="small"
              hoverable
              onClick={() =>
                setStatCardFilter(
                  statCardFilter === 'disabled' ? null : 'disabled',
                )
              }
              style={{
                cursor: 'pointer',
                ...(statCardFilter === 'disabled'
                  ? { borderColor: '#1677ff', borderWidth: 2 }
                  : {}),
              }}
            >
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>已禁用</Text>
              <div
                style={{
                  fontSize: 22,
                  fontWeight: 600,
                  color: summary.disabled > 0 ? '#faad14' : undefined,
                }}
              >
                {summary.disabled}
              </div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={12} md={5}>
          <Card
              size="small"
              hoverable
              onClick={() =>
                setStatCardFilter(
                  statCardFilter === 'fatal' ? null : 'fatal',
                )
              }
              style={{
                cursor: 'pointer',
                ...(statCardFilter === 'fatal'
                  ? { borderColor: '#1677ff', borderWidth: 2 }
                  : {}),
              }}
            >
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>致命级</Text>
              <div
                style={{
                  fontSize: 22,
                  fontWeight: 600,
                  color: summary.fatal > 0 ? '#cf1322' : undefined,
                }}
              >
                {summary.fatal}
              </div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={12} md={4}>
          <Card
              size="small"
              hoverable
              onClick={() =>
                setStatCardFilter(
                  statCardFilter === 'major' ? null : 'major',
                )
              }
              style={{
                cursor: 'pointer',
                ...(statCardFilter === 'major'
                  ? { borderColor: '#1677ff', borderWidth: 2 }
                  : {}),
              }}
            >
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>重大级</Text>
              <div style={{ fontSize: 22, fontWeight: 600 }}>
                {summary.major}
              </div>
            </div>
          </Card>
        </Col>
      </Row>

      {/* ================= 错误提示 ================= */}
      {error && (
        <Alert
          type="error"
          showIcon
          message="数据加载异常"
          description={error}
          action={
            <Button size="small" onClick={() => void loadRules()}>
              重新加载
            </Button>
          }
          closable
        />
      )}

      {/* ================= 主体双栏 ================= */}
      <Row gutter={12}>
        {/* ---------------- 左：规则树 ---------------- */}
        <Col xs={24} md={9}>
          <Card
            size="small"
            title={
              <Space size={6}>
                <SafetyCertificateOutlined />
                <Text strong style={{ fontSize: 13 }}>
                  审查规则树
                </Text>
              </Space>
            }
            extra={
              <Space size={4}>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={loadingRules}
                  onClick={() => void loadRules()}
                />
                <Button
                  size="small"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={handleOpenCreate}
                >
                  新增
                </Button>
              </Space>
            }
            styles={{ body: { padding: 12 } }}
          >
            <Input
              placeholder="搜索规则名称 / 编码 / 法律依据"
              prefix={<SearchOutlined />}
              size="small"
              allowClear
              value={treeKeyword}
              onChange={(event) => setTreeKeyword(event.target.value)}
              style={{ marginBottom: 10 }}
            />

            {treeData.length > 0 ? (
              <div style={{ maxHeight: 560, overflow: 'auto' }}>
                <Tree
                  treeData={treeData}
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys as string[])}
                  selectedKeys={
                    selectedRuleCode ? [`rule-${selectedRuleCode}`] : []
                  }
                  onSelect={(keys, info) => {
                    // 说明：分组节点不可选，避免选中分组后详情区空转
                    if (info.node.isLeaf === false) return;
                    const key = String(keys[0] ?? '');
                    selectRule(key.replace('rule-', ''));
                  }}
                  blockNode
                  showLine
                />
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {treeKeyword ? '没有匹配的规则' : '规则库为空'}
                  </Text>
                }
              />
            )}
          </Card>
        </Col>

        {/* ---------------- 右：规则详情 ---------------- */}
        <Col xs={24} md={15}>
          <Card
            size="small"
            title={
              <Space size={6}>
                <FileProtectOutlined />
                <Text strong style={{ fontSize: 13 }}>
                  规则详情
                </Text>
                {currentRule && (
                  <Tag
                    color={currentRule.enabled ? 'success' : 'default'}
                    style={{ margin: 0, fontSize: 11 }}
                  >
                    {currentRule.enabled ? '已启用' : '已禁用'}
                  </Tag>
                )}
              </Space>
            }
            extra={
              currentRule && (
                <Space size={4}>
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => handleOpenEdit(currentRule)}
                  >
                    编辑
                  </Button>
                  <Button
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => handleDelete(currentRule)}
                  >
                    删除
                  </Button>
                </Space>
              )
            }
            styles={{ body: { padding: 16 } }}
          >
            {currentRule ? (
              <div>
                {/* ---------------- 启用开关 ---------------- */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '10px 12px',
                    background: '#fafafa',
                    borderRadius: 6,
                    marginBottom: 16,
                  }}
                >
                  <div>
                    <Text strong style={{ fontSize: 13 }}>
                      {currentRule.rule_name}
                    </Text>
                    <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 2 }}>
                      <Text code style={{ fontSize: 11 }}>
                        {currentRule.rule_code}
                      </Text>
                      {' · '}版本 v{currentRule.version}
                    </div>
                  </div>
                  <Space size={8}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {currentRule.enabled ? '规则生效中' : '规则已停用'}
                    </Text>
                    <Switch
                      checked={currentRule.enabled}
                      loading={submitting}
                      onChange={(checked) =>
                        handleToggleEnabled(currentRule, checked)
                      }
                    />
                  </Space>
                </div>

                {/* ---------------- 基础信息 ---------------- */}
                <Descriptions
                  size="small"
                  column={isMobile ? 1 : 2}
                  bordered
                  style={{ marginBottom: 16 }}
                  items={[
                    {
                      key: 'severity',
                      label: '规则严重度',
                      children: (
                        <Tag
                          color={SEVERITY_COLOR[currentRule.severity] ?? 'default'}
                          style={{ margin: 0 }}
                        >
                          {RULE_SEVERITY_LABEL[
                            currentRule.severity as keyof typeof RULE_SEVERITY_LABEL
                          ] ?? currentRule.severity}
                        </Tag>
                      ),
                    },
                    {
                      key: 'riskLevel',
                      label: '定级结果',
                      children: (
                        <Tooltip title="严重度由规则引擎映射为风险等级，前端展示，不可编辑">
                          <Tag
                            color={
                              currentRule.override_risk_level
                                ? 'red'
                                : 'blue'
                            }
                            style={{ margin: 0 }}
                          >
                            {currentRule.override_risk_level
                              ? `${RISK_LEVEL_LABEL[currentRule.override_risk_level]}（显式覆盖）`
                              : '按严重度自动映射'}
                          </Tag>
                        </Tooltip>
                      ),
                    },
                    {
                      key: 'contractTypes',
                      label: '适用合同类型',
                      children: (
                        <Space size={4} wrap>
                          {currentRule.contract_types.map((type) => (
                            <Tag
                              key={type}
                              color={CONTRACT_TYPE_COLOR[type] ?? 'default'}
                              style={{ margin: 0, fontSize: 11 }}
                            >
                              {CONTRACT_TYPE_LABEL[type] ?? type}
                            </Tag>
                          ))}
                          {currentRule.contract_types.length > 1 && (
                            <Tooltip title="多类型规则在所有适用类型的审查中都会生效，修改一处即全类型生效">
                              <Text type="secondary" style={{ fontSize: 11 }}>
                                （全类型同步生效）
                              </Text>
                            </Tooltip>
                          )}
                        </Space>
                      ),
                    },
                    {
                      key: 'matchType',
                      label: '匹配方式',
                      children: (
                        <Tooltip title={MATCH_TYPE_HINT[currentRule.match_type]}>
                          <Text style={{ fontSize: 12 }}>
                            {MATCH_TYPE_LABEL[
                              currentRule.match_type as keyof typeof MATCH_TYPE_LABEL
                            ] ?? currentRule.match_type}
                          </Text>
                        </Tooltip>
                      ),
                    },
                    {
                      key: 'priority',
                      label: '优先级',
                      children: (
                        <Tooltip title="数值越大越优先，用于同一处命中多条规则时决定呈现顺序">
                          <Text style={{ fontSize: 12 }}>
                            {currentRule.priority}
                          </Text>
                        </Tooltip>
                      ),
                    },
                    {
                      key: 'description',
                      label: '风险说明',
                      span: 2,
                      children: (
                        <Text style={{ fontSize: 12, lineHeight: 1.7 }}>
                          {currentRule.description || '—'}
                        </Text>
                      ),
                    },
                  ]}
                />

                {/* ---------------- 匹配配置 ---------------- */}
                <div style={{ marginBottom: 16 }}>
                  <Text strong style={{ fontSize: 12 }}>
                    匹配条件配置
                  </Text>
                  <div
                    style={{
                      marginTop: 6,
                      padding: 12,
                      background: '#f6f8fa',
                      border: '1px solid #f0f0f0',
                      borderRadius: 4,
                      fontFamily: 'Consolas, Monaco, monospace',
                      fontSize: 12,
                      lineHeight: 1.7,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                      maxHeight: 200,
                      overflow: 'auto',
                    }}
                  >
                    {JSON.stringify(currentRule.match_config, null, 2)}
                  </div>
                </div>

                {/* ---------------- 法律依据 ---------------- */}
                <div style={{ marginBottom: 16 }}>
                  <Text strong style={{ fontSize: 12 }}>
                    法律依据
                  </Text>
                  <div
                    style={{
                      marginTop: 6,
                      padding: '10px 12px',
                      background: '#f6ffed',
                      border: '1px solid #b7eb8f',
                      borderRadius: 4,
                      fontSize: 12,
                      lineHeight: 1.8,
                    }}
                  >
                    {currentRule.legal_basis || '未填写法律依据'}
                  </div>
                </div>

                {/* ---------------- 修改建议模板 ---------------- */}
                <div>
                  <Text strong style={{ fontSize: 12 }}>
                    修改建议模板
                  </Text>
                  <div
                    style={{
                      marginTop: 6,
                      padding: '10px 12px',
                      background: `${'#1677ff'}0d`,
                      border: '1px solid #91caff',
                      borderRadius: 4,
                      fontSize: 12,
                      lineHeight: 1.8,
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {currentRule.suggestion_template ||
                      '未配置建议模板，命中该规则时将不自动生成建议文本'}
                  </div>
                </div>
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div>
                    <Title level={5} style={{ marginBottom: 4, fontSize: 14 }}>
                      请从左侧规则树中选择一条规则
                    </Title>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      选中后可查看匹配配置、法律依据与修改建议模板
                    </Text>
                  </div>
                }
              >
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={handleOpenCreate}
                >
                  新增规则
                </Button>
              </Empty>
            )}
          </Card>
        </Col>
      </Row>

      {/* ================= 示范条款库 ================= */}
      <Card
        size="small"
        title={
          <Space size={6}>
            <FileProtectOutlined />
            <Text strong style={{ fontSize: 13 }}>
              标准示范条款库
            </Text>
            <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
              共 {standardClauses.length} 条
            </Text>
          </Space>
        }
        extra={
          <Space size={8}>
            <Select
              size="small"
              placeholder="按条款类型筛选"
              allowClear
              style={{ width: 160 }}
              value={clauseTypeFilter}
              options={CLAUSE_TYPE_OPTIONS}
              onChange={(value) => {
                setClauseTypeFilter(value);
                void loadStandardClauses(value);
              }}
            />
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={loadingClauses}
              onClick={() => void loadStandardClauses(clauseTypeFilter)}
            />
            <Button
              size="small"
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                clauseForm.resetFields();
                setClauseModalOpen(true);
              }}
            >
              新增条款
            </Button>
          </Space>
        }
        styles={{ body: { padding: 0 } }}
      >
        <Table<StandardClauseResponse>
          rowKey="clause_id"
          size="small"
          columns={clauseColumns}
          dataSource={standardClauses}
          loading={loadingClauses}
          pagination={{
            pageSize: 10,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50'],
            showTotal: (count) => `共 ${count} 条示范条款`,
          }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    暂无示范条款，点击右上角新增
                  </Text>
                }
              />
            ),
          }}
        />
      </Card>

      {/* ================= 规则编辑弹窗 ================= */}
      <Modal
        title={ruleModalMode === 'create' ? '新增审查规则' : '编辑审查规则'}
        open={ruleModalMode !== null}
        width={isMobile ? '92vw' : 800}
        maskClosable={false}
        onCancel={() => setRuleModalMode(null)}
        footer={
          <Space>
            <Button onClick={() => setRuleModalMode(null)} disabled={submitting}>
              取消
            </Button>
            <Button
              type="primary"
              loading={submitting}
              onClick={handleSubmitRule}
            >
              保存规则
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16, fontSize: 12 }}
          message="规则是风险定级的唯一依据"
          description="规则一旦启用，命中即按严重度自动定级，大模型不参与定级。请谨慎调整启用状态与严重度。"
        />

        <Form form={ruleForm} layout="vertical" disabled={submitting}>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="rule_code"
                label="规则编码"
                rules={[
                  { required: true, message: '请填写规则编码' },
                  {
                    pattern: /^[A-Z][A-Z0-9-]{2,31}$/,
                    message: '大写字母开头，可含数字与连字符，3-32 位',
                  },
                ]}
                extra={
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    编码是规则的唯一标识，创建后不建议修改
                  </Text>
                }
              >
                <Input
                  placeholder="例如：PUR-005"
                  disabled={ruleModalMode === 'edit'}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="rule_name"
                label="规则名称"
                rules={[{ required: true, message: '请填写规则名称' }]}
              >
                <Input placeholder="例如：付款无验收前置" />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="description"
            label="风险说明"
            rules={[{ required: true, message: '请填写风险说明' }]}
            extra={
              <Text type="secondary" style={{ fontSize: 11 }}>
                面向法务的解释性描述，会出现在风险卡片上
              </Text>
            }
          >
            <TextArea
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="说明该条款存在什么风险、可能导致什么后果"
            />
          </Form.Item>

          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                name="match_type"
                label="匹配方式"
                rules={[{ required: true, message: '请选择匹配方式' }]}
              >
                <Select
                  options={MATCH_TYPE_OPTIONS}
                  onChange={(value) => {
                    // 说明：切换匹配方式时重置配置为对应示例，
                    // 避免残留上一次的字段导致规则不生效
                    const examples: Record<string, Record<string, unknown>> = {
                      keyword: { keywords: [] },
                      regex: { pattern: '' },
                      clause_type: { clause_type: 'liability' },
                      semantic: { query: '', threshold: 0.8 },
                      field_compare: { left_field: '', operator: 'gt', right_field: '' },
                      missing_clause: { clause_type: 'liability' },
                      numeric_threshold: {
                        field: 'payment_ratio',
                        operator: 'gt',
                        value: 100,
                      },
                    };
                    ruleForm.setFieldValue(
                      'match_config',
                      examples[value] ?? {},
                    );
                  }}
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="severity"
                label="严重度"
                rules={[{ required: true, message: '请选择严重度' }]}
                extra={
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    决定命中后的风险等级
                  </Text>
                }
              >
                <Select options={RULE_SEVERITY_OPTIONS} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="priority" label="优先级">
                <InputNumber
                  min={0}
                  max={100}
                  style={{ width: '100%' }}
                  placeholder="数值越大越优先"
                />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="contract_types"
                label="适用合同类型"
                rules={[{ required: true, message: '请选择适用的合同类型' }]}
              >
                <Select
                  mode="multiple"
                  options={RULE_CONTRACT_TYPE_OPTIONS}
                  placeholder="可选择多个类型"
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="override_risk_level"
                label="显式覆盖风险等级"
                extra={
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    留空则按严重度自动映射，仅在特殊场景使用
                  </Text>
                }
              >
                <Select
                  allowClear
                  options={RISK_LEVEL_OPTIONS}
                  placeholder="默认按严重度映射"
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="match_config"
            label="匹配条件配置（JSON）"
            rules={[
              { required: true, message: '请填写匹配条件配置' },
              {
                validator: (_rule, value) => {
                  // 校验 JSON 可解析且为对象，避免保存出无法被引擎解析的配置
                  if (value === undefined || value === null || value === '') {
                    return Promise.resolve();
                  }
                  try {
                    const parsed =
                      typeof value === 'string' ? JSON.parse(value) : value;
                    if (
                      typeof parsed !== 'object' ||
                      parsed === null ||
                      Array.isArray(parsed)
                    ) {
                      return Promise.reject(
                        new Error('配置必须是一个 JSON 对象'),
                      );
                    }
                    return Promise.resolve();
                  } catch {
                    return Promise.reject(new Error('JSON 格式有误，无法解析'));
                  }
                },
              },
            ]}
            extra={
              <Text type="secondary" style={{ fontSize: 11 }}>
                需为 JSON 对象。示例：关键词 {'{"keywords":["概不负责"]}'} ·{' '}
                正则 {'{"pattern":"付款.{0,10}100%"}'} · 缺失{' '}
                {'{"clause_type":"liability"}'}
              </Text>
            }
          >
            <TextArea
              autoSize={{ minRows: 4, maxRows: 10 }}
              style={{ fontFamily: 'Consolas, Monaco, monospace', fontSize: 12 }}
              placeholder='{"keywords": ["无条件", "概不负责"]}'
            />
          </Form.Item>

          <Form.Item name="legal_basis" label="法律依据">
            <Input placeholder="例如：《民法典》第 585 条" />
          </Form.Item>

          <Form.Item
            name="suggestion_template"
            label="修改建议模板"
            extra={
              <Text type="secondary" style={{ fontSize: 11 }}>
                命中该规则时自动生成的建议文本，应给出可直接落地的示范条款
              </Text>
            }
          >
            <TextArea
              autoSize={{ minRows: 3, maxRows: 8 }}
              placeholder='例如：建议约定「甲方应在收到乙方交付成果后 10 个工作日内完成验收，逾期未验收视为验收通过」'
            />
          </Form.Item>

          <Form.Item name="enabled" label="创建后立即启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* ================= 示范条款新增弹窗 ================= */}
      <Modal
        title="新增标准示范条款"
        open={clauseModalOpen}
        width={isMobile ? '92vw' : 720}
        maskClosable={false}
        onCancel={() => setClauseModalOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setClauseModalOpen(false)} disabled={submitting}>
              取消
            </Button>
            <Button type="primary" loading={submitting} onClick={handleSubmitClause}>
              保存条款
            </Button>
          </Space>
        }
      >
        <Form form={clauseForm} layout="vertical" disabled={submitting}>
          <Form.Item
            name="clause_title"
            label="条款名称"
            rules={[{ required: true, message: '请填写条款名称' }]}
          >
            <Input placeholder="例如：交付与验收标准条款" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="clause_type"
                label="条款类型"
                rules={[{ required: true, message: '请选择条款类型' }]}
              >
                <Select options={CLAUSE_TYPE_OPTIONS} placeholder="请选择" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="contract_types"
                label="适用合同类型"
                rules={[{ required: true, message: '请选择适用的合同类型' }]}
              >
                <Select
                  mode="multiple"
                  options={RULE_CONTRACT_TYPE_OPTIONS}
                  placeholder="可选择多个类型"
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="clause_text"
            label="条款正文"
            rules={[
              { required: true, message: '请填写条款正文' },
              { min: 20, message: '条款正文至少 20 字，需具备可执行性' },
            ]}
            extra={
              <Text type="secondary" style={{ fontSize: 11 }}>
                应包含明确的责任主体、时间节点与判定标准，避免出现"另行协商""适时"等模糊表述
              </Text>
            }
          >
            <TextArea
              autoSize={{ minRows: 6, maxRows: 14 }}
              placeholder="请填写完整的示范条款正文…"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
