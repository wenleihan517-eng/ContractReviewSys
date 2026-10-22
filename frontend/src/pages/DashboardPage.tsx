// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/DashboardPage.tsx
/**
 * 页面一：合同审查大盘
 *
 * 【页面职责】
 *   全量待审合同的总览与调度中心，承担四项核心能力：
 *     1. 待审合同列表：合同名称、申请人、业务类型、合同金额、审查状态、
 *        综合风险等级、创建时间
 *     2. 多条件筛选：风险等级 / 任务状态 / 合同类型 / 关键字
 *     3. 手动上传入口、批量操作、阻塞任务重试
 *     4. 长任务实时进度条（进行中的任务按固定间隔轮询刷新）
 *
 * 【数据来源说明】
 *   后端任务列表接口只返回任务基础字段，不含申请人/业务类型/金额/风险等级。
 *   这些字段由 store 在列表加载后并发拉取任务详情聚合补齐。
 *   若后端未提供某字段，界面展示占位符「—」而非编造数据——
 *   在合规审查场景中，展示一个不存在的金额比展示空值危险得多。
 *
 * 【风险等级只读】
 *   风险等级由后端规则引擎产出，本页仅做展示与排序，
 *   不提供任何修改入口。
 */

import { useEffect, useMemo, useState } from 'react';
import '@/styles/dashboard-table.css';
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Input,
  Pagination,
  Progress,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CloudUploadOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

import UploadContractModal from '@/components/UploadContractModal';
import {
  BLOCKED_REASON_LABEL,
  BUSINESS_TYPE_LABEL,
  CONTRACT_TYPE_OPTIONS,
  FAILURE_TYPE_LABEL,
  FAILURE_TYPE_OPTIONS,
  PAGINATION,
  RISK_LEVEL_OPTIONS,
  RISK_LEVEL_TAG_COLOR,
  TASK_STATUS,
  TASK_STATUS_BADGE,
  TASK_STATUS_IN_PROGRESS,
  TASK_STATUS_OPTIONS,
  THEME,
  type TaskStatus,
} from '@/constants';
import { useAuthStore } from '@/stores/authStore';
import { useResponsive } from '@/hooks/useResponsive';
import {
  selectDashboardSummary,
  useTaskStore,
} from '@/stores/taskStore';
import {
  formatAmount,
  formatTime,
  riskLevelLabel,
  taskStatusLabel,
  truncate,
} from '@/utils';
import type { DashboardRow } from '@/types';

const { Text } = Typography;

/** 可重跑的任务状态集合 */
const RETRYABLE_STATUSES: TaskStatus[] = [
  TASK_STATUS.FAILED,
  TASK_STATUS.BLOCKED,
  TASK_STATUS.CANCELLED,
];

/**
 * 已有审查结论、可查看报告的任务状态集合
 *
 * 说明：
 *   - rule_done    规则引擎已判定（结论已产出，报告可生成）
 *   - blocked      被规则拦截，但结论与风险项已落库（更应让经办人看到原因）
 *   - completed    全流程完成
 *   进行中（pending/running/llm_done）尚无结论，不展示入口，
 *   避免点进去只看到"审查尚未完成"的空页面。
 */
const REPORT_VIEWABLE_STATUSES: TaskStatus[] = [
  TASK_STATUS.RULE_DONE,
  TASK_STATUS.BLOCKED,
  TASK_STATUS.COMPLETED,
];

/** 路由类型展示配置 */
const ROUTE_TYPE_CONFIG: Record<string, { label: string; color: string }> = {
  fast: { label: '快速通道', color: 'cyan' },
  standard: { label: '标准审查', color: 'blue' },
  deep: { label: '深度复核', color: 'purple' },
};

/** 快捷筛选预设 */
const QUICK_FILTERS = [
  { label: '全部', value: 'all' },
  { label: '进行中', value: 'running' },
  { label: '已阻塞', value: 'blocked' },
  { label: '高风险及以上', value: 'high' },
  { label: '已完成', value: 'completed' },
  { label: '异常任务', value: 'failed' },
] as const;

type QuickFilter = (typeof QUICK_FILTERS)[number]['value'];

export default function DashboardPage(): React.ReactElement {
  const navigate = useNavigate();
  const { message, modal } = App.useApp();

  // ---------------- Store 状态 ----------------
  const rows = useTaskStore((state) => state.rows);
  const loading = useTaskStore((state) => state.loading);
  const error = useTaskStore((state) => state.error);
  const filters = useTaskStore((state) => state.filters);
  const page = useTaskStore((state) => state.page);
  const pageSize = useTaskStore((state) => state.pageSize);
  const total = useTaskStore((state) => state.total);
  const selectedTaskNos = useTaskStore((state) => state.selectedTaskNos);
  const actioningTaskNos = useTaskStore((state) => state.actioningTaskNos);

  const setFilters = useTaskStore((state) => state.setFilters);
  const resetFilters = useTaskStore((state) => state.resetFilters);
  const setPage = useTaskStore((state) => state.setPage);
  const loadTasks = useTaskStore((state) => state.loadTasks);
  const setSelected = useTaskStore((state) => state.setSelected);
  const retryTask = useTaskStore((state) => state.retryTask);
  const replaceFile = useTaskStore((state) => state.replaceFile);
  const batchRetry = useTaskStore((state) => state.batchRetry);
  const stopPolling = useTaskStore((state) => state.stopPolling);
  const user = useAuthStore((state) => state.user);
  const { isMobile } = useResponsive();

  // ---------------- 本地 UI 状态 ----------------
  const [uploadOpen, setUploadOpen] = useState(false);
  const [quickFilter, setQuickFilter] = useState<QuickFilter>('all');
  /** 关键字输入框的受控值（回车或失焦时才提交到 store，避免逐字符触发请求） */
  const [keywordDraft, setKeywordDraft] = useState('');

  // ---------------- 首屏加载与清理 ----------------
  useEffect(() => {
    void loadTasks();
    // 组件卸载时停止轮询，避免离开页面后仍在后台空转
    return () => stopPolling();
  }, [loadTasks, stopPolling]);

  // ---------------- 派生统计 ----------------
  const summary = useMemo(() => selectDashboardSummary(rows), [rows]);

  /**
   * 应用快捷筛选
   *
   * 说明：快捷筛选是"组合条件"的语义（例如"高风险及以上"包含 high 与 critical），
   * 无法用单个下拉框表达，因此单独维护一个快捷维度，
   * 与明细筛选条件叠加生效。
   */
  const displayedRows = useMemo<DashboardRow[]>(() => {
    if (quickFilter === 'all') return rows;

    if (quickFilter === 'running') {
      return rows.filter((row) => TASK_STATUS_IN_PROGRESS.includes(row.status));
    }
    if (quickFilter === 'blocked') {
      return rows.filter((row) => row.status === TASK_STATUS.BLOCKED);
    }
    if (quickFilter === 'high') {
      return rows.filter(
        (row) => row.risk_level === 'high' || row.risk_level === 'critical',
      );
    }
    if (quickFilter === 'completed') {
      return rows.filter((row) => row.status === TASK_STATUS.COMPLETED);
    }
    if (quickFilter === 'failed') {
      return rows.filter((row) => row.status === TASK_STATUS.FAILED);
    }
    return rows;
  }, [rows, quickFilter]);

  /**
   * 当前页可批量操作的任务编号
   *
   * 说明：只有失败、阻塞或已取消的任务可以重试，
   * 进行中与已完成的任务不应出现在批量重试范围内。
   */
  const retryableTaskNos = useMemo(
    () =>
      displayedRows
        .filter((row) => RETRYABLE_STATUSES.includes(row.status))
        .map((row) => row.task_no),
    [displayedRows],
  );

  /** 选中项中可重试的编号 */
  const selectedRetryable = useMemo(
    () => selectedTaskNos.filter((taskNo) => retryableTaskNos.includes(taskNo)),
    [selectedTaskNos, retryableTaskNos],
  );

  // ---------------- 交互处理 ----------------

  /** 提交关键字搜索 */
  const commitKeyword = (): void => {
    if (keywordDraft.trim() === filters.keyword.trim()) return;
    setFilters({ keyword: keywordDraft.trim() });
  };

  /** 进入工作台 */
  const openWorkbench = (taskNo: string): void => {
    navigate(`/workbench/${taskNo}`);
  };

  /**
   * 已知任务的默认去向（按角色分流）
   *
   * 参数：
   *   taskNo: 任务编号
   *
   * 说明：
   *   经办人无工作台权限，统一去只读报告页；法务/管理员去工作台。
   *   抽成单一函数，避免"合同名称点击"与"操作按钮"两处判断不一致，
   *   导致某一处漏改又把经办人弹回大盘。
   */
  const openTaskDefault = (taskNo: string): void => {
    if (user?.role === 'law_user' || user?.role === 'admin') {
      openWorkbench(taskNo);
    } else {
      openReport(taskNo);
    }
  };

  /**
   * 进入只读审查报告页
   *
   * 参数：
   *   taskNo: 任务编号
   *
   * 说明：
   *   报告页对三种角色均开放，数据可见范围由后端按"任务提交人/法务/管理员"裁定。
   *   经办人走这里查看结果——工作台含风险项编辑与回写能力，对经办人是越权的。
   */
  const openReport = (taskNo: string): void => {
    navigate(`/report/${taskNo}`);
  };

  /** 单任务重试（含阻塞任务的二次确认） */
  const handleRetry = (row: DashboardRow): void => {
    if (row.status === TASK_STATUS.BLOCKED) {
      modal.confirm({
        title: '确认重跑该阻塞任务？',
        icon: <ExclamationCircleOutlined />,
        content: (
          <div style={{ fontSize: 13, lineHeight: 1.7 }}>
            <div>
              合同：<Text strong>{row.contract_title}</Text>
            </div>
            <div>
              阻塞原因：
              <Text type="danger">
                {row.blocked_reason ?? '未标注'}
              </Text>
            </div>
            <div style={{ marginTop: 8, color: '#8c8c8c' }}>
              重跑将从阻塞所在阶段继续执行。若阻塞原因为文档本身问题
              （如加密、模糊），重跑仍会再次阻塞，建议先处理源文件。
            </div>
          </div>
        ),
        okText: '确认重跑',
        cancelText: '取消',
        onOk: async () => {
          try {
            await retryTask(row.task_no);
            message.success('任务已重新排队');
          } catch {
            message.error('重跑失败，请稍后重试');
          }
        },
      });
      return;
    }

    void (async () => {
      try {
        await retryTask(row.task_no);
        message.success('任务已重新排队');
      } catch {
        message.error('重试失败，请稍后重试');
      }
    })();
  };

  /** 重新上传替换合同文件（仅失败任务，提交经办人/管理员） */
  const handleReplaceFile = (row: DashboardRow, file: File): void => {
    void (async () => {
      try {
        await replaceFile(row.task_no, file);
        message.success('合同文件已替换并重新提交审查');
      } catch (e) {
        message.error(e instanceof Error ? e.message : '重新上传失败，请重试');
      }
    })();
  };

  /** 批量重试 */
  const handleBatchRetry = (): void => {
    if (selectedRetryable.length === 0) {
      message.warning('请先勾选可重试的任务（失败 / 已阻塞 / 已取消）');
      return;
    }

    modal.confirm({
      title: `确认批量重跑 ${selectedRetryable.length} 个任务？`,
      content:
        '批量重跑将按顺序逐个提交，单个任务失败不影响其余任务继续执行。',
      okText: '确认执行',
      cancelText: '取消',
      onOk: async () => {
        const result = await batchRetry(selectedRetryable);
        if (result.failed === 0) {
          message.success(`已成功提交 ${result.succeeded} 个任务重跑`);
        } else {
          message.warning(
            `提交完成：成功 ${result.succeeded} 个，失败 ${result.failed} 个`,
          );
        }
        setSelected([]);
      },
    });
  };

  /**
   * 行操作按钮组（表格操作列与移动端卡片列表共用）
   *
   * 参数：
   *   row: 任务行
   *
   * 返回：
   *   操作按钮集合
   */
  const renderActions = (row: DashboardRow): React.ReactNode => {
    const actioning = actioningTaskNos.includes(row.task_no);
    const canRetry = RETRYABLE_STATUSES.includes(row.status);
    const isFailed = row.status === TASK_STATUS.FAILED;
    const isBlockedRow = row.status === TASK_STATUS.BLOCKED;
    // 重新上传权限：执行失败 或 已阻塞 的任务均可替换文件
    //（阻塞任务往往因文件内容缺陷被规则引擎拦截，重跑无效，需上传修正版）；
    // 仅提交经办人 / 管理员可操作，审查员不可替换原文件（权责分离）
    const canReplace =
      (isFailed || isBlockedRow) &&
      (user?.role === 'admin' ||
        (row.created_by && row.created_by === user?.username));

    /**
     * 主入口按钮的角色分流（2026-09-20 修复）
     *
     * 背景：原实现对所有角色一律跳 /workbench，而该路由对 business_user
     *       是禁止的（工作台含风险定级明细与法务批注），经办人点「审查」
     *       会被角色守卫弹回大盘并弹"无访问权限"——按钮可见却不可用。
     *
     * 现按角色分流（与 openTaskDefault 同源，避免两处判断漂移）：
     *   经办人 → 只读报告页 /report/{taskNo}（含结论、风险摘要、报告下载）
     *   法务/管理员 → 工作台 /workbench/{taskNo}（可复核、批注、回写）
     * 这样既消除了"点了被弹回"的坏体验，又守住了权责边界：
     * 经办人始终拿不到任何写操作入口。
     */
    const canUseWorkbench = user?.role === 'law_user' || user?.role === 'admin';
    const isDone = row.status === TASK_STATUS.COMPLETED;
    // 主按钮文案：经办人进的是报告页，文案要说清"能看什么"。
    // 用 hasReport（rule_done / blocked / completed）而非仅 completed 判定 ——
    // 阻塞任务的规则结论与风险项已落库、报告页可正常展示，
    // 若仍显示"查看进度"会低估可获取的信息（实际能看到完整风险清单）。
    const hasReport = REPORT_VIEWABLE_STATUSES.includes(row.status);
    const primaryLabel = canUseWorkbench
      ? '审查'
      : hasReport
        ? '查看报告'
        : '查看进度';

    return (
      <Space size={4} wrap>
        <Button
          type="link"
          size="small"
          style={{ padding: '0 4px' }}
          onClick={() => openTaskDefault(row.task_no)}
        >
          {primaryLabel}
        </Button>
        {/* 已完成任务：法务/管理员额外提供报告入口（与工作台内报告抽屉等价） */}
        {canUseWorkbench && isDone && (
          <Button
            type="link"
            size="small"
            style={{ padding: '0 4px' }}
            onClick={() => openReport(row.task_no)}
          >
            报告
          </Button>
        )}
        {canRetry && (
          <Button
            type="link"
            size="small"
            style={{ padding: '0 4px' }}
            loading={actioning}
            icon={<SyncOutlined />}
            onClick={() => handleRetry(row)}
          >
            {row.status === TASK_STATUS.BLOCKED ? '重跑' : '重试'}
          </Button>
        )}
        {canReplace && (
          <Upload
            showUploadList={false}
            accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp"
            beforeUpload={(file) => {
              // 拦截自动上传，改由 replaceFile 走业务接口替换文件
              handleReplaceFile(row, file as unknown as File);
              return false;
            }}
          >
            <Button
              type="link"
              size="small"
              style={{ padding: '0 4px' }}
              loading={actioning}
            >
              重新上传
            </Button>
          </Upload>
        )}
      </Space>
    );
  };

  // ---------------- 表格列定义 ----------------
  const columns: ColumnsType<DashboardRow> = [
    {
      title: '合同名称',
      dataIndex: 'contract_title',
      key: 'contract_title',
      width: 260,
      fixed: 'left',
      render: (_value, row) => (
        <div>
          <div>
            <Text
              strong
              ellipsis={{ tooltip: row.contract_title }}
              style={{ cursor: 'pointer', maxWidth: 232, display: 'inline-block' }}
              onClick={() => openTaskDefault(row.task_no)}
            >
              {truncate(row.contract_title, 28)}
            </Text>
          </div>
          <div style={{ fontSize: 12, color: '#8c8c8c' }}>
            {row.business_type && row.business_type !== '—' && (
              <Tag
                color="default"
                style={{ margin: '0 6px 0 0', fontSize: 11 }}
              >
                {BUSINESS_TYPE_LABEL[row.business_type] ?? row.business_type}
              </Tag>
            )}
            {row.contract_no || row.task_no}
          </div>
        </div>
      ),
    },
    {
      title: '申请人',
      dataIndex: 'applicant',
      key: 'applicant',
      width: 110,
      render: (value: string, row) => (
        <div>
          <div style={{ fontSize: 13 }}>{value || '—'}</div>
          {row.submit_dept && row.submit_dept !== '—' && (
            <div style={{ fontSize: 12, color: '#8c8c8c' }}>
              {row.submit_dept}
            </div>
          )}
        </div>
      ),
    },
    {
      title: '合同金额',
      dataIndex: 'contract_amount',
      key: 'contract_amount',
      width: 130,
      align: 'right',
      render: (value: number, row) => (
        <Text strong={value > 0} type={value > 0 ? undefined : 'secondary'}>
          {formatAmount(value, row.currency)}
        </Text>
      ),
    },
    {
      title: '审查状态',
      dataIndex: 'status',
      key: 'status',
      width: 150,
      render: (_value, row) => {
        const inProgress = TASK_STATUS_IN_PROGRESS.includes(row.status);
        const isFailed = row.status === TASK_STATUS.FAILED;

        // 失败态：标红 + 悬浮显示失败类型/原因与操作指引
        const badge = (
          <Badge
            status={TASK_STATUS_BADGE[row.status] ?? 'default'}
            text={
              <span style={isFailed ? { color: '#cf1322', fontWeight: 600 } : undefined}>
                {taskStatusLabel(row.status)}
              </span>
            }
          />
        );

        return (
          <div>
            {isFailed ? (
              <Tooltip
                placement="top"
                title={
                  <div style={{ fontSize: 12, lineHeight: 1.6 }}>
                    <div>
                      失败类型：
                      {FAILURE_TYPE_LABEL[row.failure_type ?? 'other'] ?? row.failure_type}
                    </div>
                    <div>失败原因：{row.failure_reason ?? '未知'}</div>
                    <div style={{ marginTop: 4, color: '#ffd591' }}>
                      瞬时异常可直接一键重试；文件损坏请重新上传替换合同文件
                    </div>
                  </div>
                }
              >
                {badge}
              </Tooltip>
            ) : (
              badge
            )}
            {/* 长任务实时进度条：仅在任务进行中时展示，避免静态任务干扰视觉 */}
            {inProgress && (
              <Progress
                percent={row.progress}
                size="small"
                style={{ marginTop: 4, marginBottom: 0 }}
                strokeColor={{ from: '#108ee9', to: '#87d068' }}
              />
            )}
            {/* 阻塞任务直接展示原因码，免除点击进入才能看到原因的成本 */}
            {row.status === TASK_STATUS.BLOCKED && row.blocked_reason && (
              <div style={{ fontSize: 11, color: '#fa8c16', marginTop: 2 }}>
                {BLOCKED_REASON_LABEL[row.blocked_reason] ?? row.blocked_reason}
              </div>
            )}
          </div>
        );
      },
    },
    {
      title: '综合风险等级',
      dataIndex: 'risk_level',
      key: 'risk_level',
      width: 120,
      align: 'center',
      // 说明：按风险等级排序时未知等级排最后，避免"未判定"混在低风险里被忽略
      sorter: (a, b) => {
        const order: Record<string, number> = {
          critical: 4,
          high: 3,
          medium: 2,
          low: 1,
        };
        return (order[a.risk_level ?? ''] ?? 0) - (order[b.risk_level ?? ''] ?? 0);
      },
      render: (value: DashboardRow['risk_level']) => {
        if (!value) {
          return (
            <Tooltip title="规则引擎尚未产出审查结论">
              <Tag style={{ margin: 0 }}>未判定</Tag>
            </Tooltip>
          );
        }
        return (
          <Tag
            color={RISK_LEVEL_TAG_COLOR[value]}
            style={{ margin: 0, fontWeight: 600 }}
          >
            {riskLevelLabel(value)}
          </Tag>
        );
      },
    },
    {
      title: '审查通道',
      dataIndex: 'route_type',
      key: 'route_type',
      width: 100,
      align: 'center',
      render: (value: string) => {
        const config = ROUTE_TYPE_CONFIG[value];
        if (!config) return <Text type="secondary">—</Text>;
        return (
          <Tag color={config.color} style={{ margin: 0 }}>
            {config.label}
          </Tag>
        );
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      sorter: (a, b) =>
        new Date(a.created_at ?? 0).getTime() -
        new Date(b.created_at ?? 0).getTime(),
      defaultSortOrder: 'descend',
      render: (value: string | null) => (
        <span style={{ fontSize: 12 }}>{formatTime(value)}</span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 230,
      fixed: 'right',
      render: (_value, row) => renderActions(row),
    },
  ];

  return (
    <div>
      {/* ================= 顶部统计卡片（点击卡片切入对应筛选维度） ================= */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={8} md={8} lg={4}>
          <Card
            size="small"
            variant="outlined"
            hoverable
            onClick={() => setQuickFilter('all')}
            style={STAT_CARD_STYLE(quickFilter === 'all')}
          >
            <Statistic
              title="待审合同总数"
              value={summary.total}
              valueStyle={{ fontSize: 24 }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} md={8} lg={4}>
          <Card
            size="small"
            variant="outlined"
            hoverable
            onClick={() => setQuickFilter('high')}
            style={STAT_CARD_STYLE(quickFilter === 'high')}
          >
            <Statistic
              title="严重 / 高风险"
              value={summary.critical + summary.high}
              valueStyle={{
                fontSize: 24,
                color: summary.critical + summary.high > 0 ? '#cf1322' : undefined,
              }}
              prefix={<ThunderboltOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} md={8} lg={4}>
          <Card
            size="small"
            variant="outlined"
            hoverable
            onClick={() => setQuickFilter('blocked')}
            style={STAT_CARD_STYLE(quickFilter === 'blocked')}
          >
            <Statistic
              title="阻塞待处理"
              value={summary.blocked}
              valueStyle={{
                fontSize: 24,
                color: summary.blocked > 0 ? '#fa8c16' : undefined,
              }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} md={8} lg={4}>
          <Card
            size="small"
            variant="outlined"
            hoverable
            onClick={() => setQuickFilter('failed')}
            style={STAT_CARD_STYLE(quickFilter === 'failed')}
          >
            <Statistic
              title="执行失败"
              value={summary.failed}
              valueStyle={{
                fontSize: 24,
                color: summary.failed > 0 ? '#cf1322' : undefined,
              }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} md={8} lg={4}>
          <Card
            size="small"
            variant="outlined"
            hoverable
            onClick={() => setQuickFilter('completed')}
            style={STAT_CARD_STYLE(quickFilter === 'completed')}
          >
            <Statistic
              title="审查已完成"
              value={summary.completed}
              valueStyle={{ fontSize: 24, color: '#52c41a' }}
            />
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
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={() => void loadTasks()}>
              重新加载
            </Button>
          }
          closable
        />
      )}

      {/* ================= 筛选与操作区 ================= */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ marginBottom: 10 }}>
          <Text strong style={{ fontSize: 13 }}>
            明细筛选
          </Text>
        </div>
        <Space
          wrap
          size={12}
          style={{ width: '100%', justifyContent: 'space-between' }}
        >
          <Space wrap size={12}>
            <Input
              placeholder="搜索合同名称 / 编号 / 申请人"
              prefix={<SearchOutlined />}
              style={{ width: 260 }}
              value={keywordDraft}
              allowClear
              onChange={(event) => {
                const next = event.target.value;
                setKeywordDraft(next);
                // 清空输入框时立即复位筛选，符合直觉
                if (next === '') setFilters({ keyword: '' });
              }}
              onPressEnter={commitKeyword}
              onBlur={commitKeyword}
            />

            <Select
              placeholder="任务状态"
              style={{ width: 150 }}
              allowClear
              value={filters.status}
              options={TASK_STATUS_OPTIONS}
              onChange={(value) => setFilters({ status: value ?? null })}
            />

            <Select
              placeholder="风险等级"
              style={{ width: 140 }}
              allowClear
              value={filters.riskLevel}
              options={RISK_LEVEL_OPTIONS}
              onChange={(value) => setFilters({ riskLevel: value ?? null })}
            />

            <Select
              placeholder="合同类型"
              style={{ width: 140 }}
              allowClear
              value={filters.contractType}
              options={CONTRACT_TYPE_OPTIONS}
              onChange={(value) => setFilters({ contractType: value ?? null })}
            />

            <Select
              placeholder="失败类型"
              style={{ width: 150 }}
              allowClear
              value={filters.failureType}
              options={FAILURE_TYPE_OPTIONS}
              onChange={(value) => setFilters({ failureType: value ?? null })}
            />

            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                setKeywordDraft('');
                resetFilters();
              }}
            >
              重置
            </Button>
          </Space>

          <Space size={8}>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void loadTasks()}
              loading={loading}
            >
              刷新
            </Button>
            <Button
              icon={<SyncOutlined />}
              disabled={selectedRetryable.length === 0}
              onClick={handleBatchRetry}
            >
              批量重跑
              {selectedRetryable.length > 0 && ` (${selectedRetryable.length})`}
            </Button>
            <Button
              type="primary"
              icon={<CloudUploadOutlined />}
              onClick={() => setUploadOpen(true)}
            >
              上传合同送审
            </Button>
          </Space>
        </Space>

        {/* ---------------- 快捷筛选（与上方明细筛选叠加生效） ---------------- */}
        <div
          style={{
            marginTop: 12,
            paddingTop: 12,
            borderTop: `1px solid ${THEME.BORDER_SECONDARY}`,
          }}
        >
          <Space size={8} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              快捷筛选
            </Text>
            <Segmented
              value={quickFilter}
              onChange={(value) => setQuickFilter(value as QuickFilter)}
              options={QUICK_FILTERS.map((item) => ({
                label: item.label,
                value: item.value,
              }))}
            />
          </Space>
          {quickFilter !== 'all' && (
            <Text type="secondary" style={{ marginLeft: 12, fontSize: 12 }}>
              已按快捷条件过滤，当前展示 {displayedRows.length} 条
            </Text>
          )}
        </div>
      </Card>

      {/* ================= 任务列表 ================= */}
      <Card
        size="small"
        styles={{ body: { padding: isMobile ? 12 : 0 } }}
      >
        {/* ---------------- 移动端：卡片式列表（完整信息，免横向滚动） ---------------- */}
        {isMobile && (
          <>
            {loading && (
              <div style={{ textAlign: 'center', padding: 24 }}>
                <Spin />
              </div>
            )}
            {!loading && displayedRows.length === 0 && (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  filters.keyword ||
                  filters.status ||
                  filters.riskLevel ||
                  filters.contractType ||
                  filters.failureType ||
                  quickFilter !== 'all'
                    ? '没有符合筛选条件的合同'
                    : '暂无待审合同，点击右上角上传送审'
                }
                style={{ padding: '24px 0' }}
              />
            )}
            {displayedRows.map((row) => {
              const isFailed = row.status === TASK_STATUS.FAILED;
              return (
                <Card
                  key={row.task_no}
                  size="small"
                  style={{
                    marginBottom: 8,
                    ...(isFailed
                      ? { borderColor: '#ffccc7', background: '#fff1f0' }
                      : {}),
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: 8,
                    }}
                  >
                    <div style={{ minWidth: 0, flex: 1 }}>
                      {/* 名称与编号：完整展示，长名称自动折行 */}
                      <div style={{ wordBreak: 'break-all' }}>
                        <Text strong style={{ fontSize: 14 }}>
                          {row.contract_title}
                        </Text>
                      </div>
                      <Text
                        type="secondary"
                        style={{ fontSize: 11, wordBreak: 'break-all' }}
                      >
                        {row.task_no}
                      </Text>
                      <div style={{ marginTop: 6 }}>
                        <Badge
                          status={TASK_STATUS_BADGE[row.status] ?? 'default'}
                          text={taskStatusLabel(row.status)}
                        />
                      </div>
                      {isFailed && row.failure_reason && (
                        <div
                          style={{
                            fontSize: 11,
                            color: '#cf1322',
                            marginTop: 4,
                          }}
                        >
                          {row.failure_reason}
                        </div>
                      )}
                    </div>
                    <div style={{ flexShrink: 0, textAlign: 'right' }}>
                      {row.risk_level && (
                        <Tag
                          color={RISK_LEVEL_TAG_COLOR[row.risk_level]}
                          style={{ margin: 0, fontSize: 11 }}
                        >
                          {riskLevelLabel(row.risk_level)}
                        </Tag>
                      )}
                      <Text
                        type="secondary"
                        style={{
                          display: 'block',
                          fontSize: 11,
                          marginTop: 6,
                        }}
                      >
                        {formatTime(row.created_at, 'MM-DD HH:mm')}
                      </Text>
                    </div>
                  </div>
                  {/* 操作按钮区：与桌面表格操作列完全同源 */}
                  <div
                    style={{
                      marginTop: 8,
                      paddingTop: 8,
                      borderTop: '1px solid #f5f5f5',
                    }}
                  >
                    {renderActions(row)}
                  </div>
                </Card>
              );
            })}
            <Pagination
              current={page}
              pageSize={pageSize}
              total={total}
              size="small"
              style={{ textAlign: 'center', marginTop: 8 }}
              showTotal={(count) => `共 ${count} 条`}
              onChange={(nextPage, nextPageSize) =>
                setPage(nextPage, nextPageSize)
              }
            />
          </>
        )}

        {/* ---------------- 桌面端：表格 ---------------- */}
        {!isMobile && (
          <>
          <Table<DashboardRow>
          rowKey="task_no"
          rowClassName={(row) =>
            row.risk_level === 'critical' ? 'row-risk-critical' : ''
          }
          columns={columns}
          dataSource={displayedRows}
          loading={loading}
          scroll={{ x: 1260 }}
          size="middle"
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  filters.keyword ||
                  filters.status ||
                  filters.riskLevel ||
                  filters.contractType ||
                  quickFilter !== 'all'
                    ? '没有符合筛选条件的合同'
                    : '暂无待审合同，点击右上角上传送审'
                }
              />
            ),
          }}
          rowSelection={{
            selectedRowKeys: selectedTaskNos,
            onChange: (keys) => setSelected(keys as string[]),
            // 说明：只允许勾选可重试的任务，避免误勾进行中的任务后批量操作失效
            getCheckboxProps: (row) => ({
              disabled: !retryableTaskNos.includes(row.task_no),
            }),
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            size: 'default',
            showSizeChanger: true,
            pageSizeOptions: [...PAGINATION.PAGE_SIZE_OPTIONS],
            showTotal: (count, range) =>
              `第 ${range[0]}-${range[1]} 条 / 共 ${count} 条`,
            onChange: (nextPage, nextPageSize) =>
              setPage(nextPage, nextPageSize),
          }}
        />

        {/* ---------------- 全选辅助条（仅桌面端） ---------------- */}
        {displayedRows.length > 0 && (
          <div
            style={{
              padding: '10px 16px',
              borderTop: '1px solid #f0f0f0',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <Checkbox
              indeterminate={
                selectedTaskNos.length > 0 &&
                selectedTaskNos.length < retryableTaskNos.length
              }
              checked={
                retryableTaskNos.length > 0 &&
                selectedTaskNos.length === retryableTaskNos.length
              }
              onChange={(event) =>
                setSelected(event.target.checked ? retryableTaskNos : [])
              }
            >
              全选本页可重试任务
            </Checkbox>
            <Text type="secondary" style={{ fontSize: 12 }}>
              已选 {selectedTaskNos.length} 条 · 本页可重跑{' '}
              {retryableTaskNos.length} 条
            </Text>
          </div>
        )}
          </>
        )}
      </Card>

      {/* ================= 上传弹窗 ================= */}
      <UploadContractModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onSuccess={(taskNo) => {
          // 上传成功后刷新列表，让新任务立刻出现在列表中
          void loadTasks();
          // 引导使用者直接进入可用的作业面。
          // 注意：这里同样按角色分流——经办人不能进工作台（会被守卫弹回），
          // 因此文案与目标都要跟着角色走，否则又是一个"点了被弹回"的入口。
          const toWorkbench =
            user?.role === 'law_user' || user?.role === 'admin';
          modal.confirm({
            title: '合同已提交审查',
            content: toWorkbench
              ? `任务编号 ${taskNo} 已进入审查流水线，是否立即进入工作台查看？`
              : `任务编号 ${taskNo} 已进入审查流水线，审查完成后可在「审查报告」中查看结论与下载报告。`,
            okText: toWorkbench ? '进入工作台' : '查看任务',
            cancelText: '留在列表',
            onOk: () => openTaskDefault(taskNo),
          });
        }}
      />
    </div>
  );
}

/** 统计卡样式：可点击切入对应筛选维度，激活态用主色描边 + 浅蓝底 */
const STAT_CARD_STYLE = (active: boolean): React.CSSProperties =>
  active
    ? {
        cursor: 'pointer',
        borderColor: '#1677ff',
        background: '#e6f4ff',
        boxShadow: '0 0 0 2px rgba(22,119,255,0.15)',
      }
    : { cursor: 'pointer' };
