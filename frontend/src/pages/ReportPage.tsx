// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/ReportPage.tsx
/**
 * 审查报告页（只读）
 *
 * 【文件职责】
 *   面向「业务经办人」为主的只读报告视图，展示其提交合同的：
 *     - 审查结论（规则引擎终裁的风险等级与结论文本）
 *     - 风险摘要（按等级分布统计 + 风险项清单，仅读）
 *     - 报告下载（Markdown / PDF）
 *
 * 【与工作台(WorkbenchPage)的边界（权责红线）】
 *   本页面**刻意不含**以下能力，这是设计约束而非尚未实现：
 *     1. 风险项人工编辑（risk_item:edit）—— 属法务职责
 *     2. 人工批注 / 法务意见（comment:annotate）—— 属法务职责
 *     3. 审批回写（approval:write_back）—— 属法务职责
 *     4. 触发审查 / 重跑 —— 属系统调度与管理员职责
 *   因此本页不复用工作台的 store 与底部操作面板，
 *   避免"引用了带写操作的组件，日后误加按钮就变成越权入口"。
 *
 * 【为什么单独建页而不给工作台加只读模式】
 *   只读模式需要靠条件渲染逐处隐藏按钮，任何一处漏判即越权；
 *   独立页面从组件层面就不存在写操作代码，是最可靠的隔离方式。
 *
 * 【数据来源与权限】
 *   - GET /tasks/{taskNo}          任务详情（标题、状态、创建时间）
 *   - GET /review/{taskNo}/result  审查结果（结论 + 风险项，规则引擎终裁）
 *   - GET /report/{taskNo}/download 报告原文（Markdown）
 *   可见范围由后端裁定：经办人仅本人任务，他人任务返回 403（页面展示无权限态）。
 */

import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ArrowLeftOutlined,
  DownloadOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';

import { reportApi, reviewApi, taskApi } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import {
  RISK_LEVEL_COLOR,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_ORDER,
  RISK_LEVEL_TAG_COLOR,
  TASK_STATUS,
} from '@/constants';
import { useResponsive } from '@/hooks/useResponsive';
import { formatTime, taskStatusLabel } from '@/utils';
import type { RiskItemResponse, TaskDetailResponse } from '@/types';
// RiskLevel 在 @/types 中仅被内部 import 使用（未 re-export），
// 因此类型来源必须显式取自 @/constants —— 那里才是该类型的定义处。
import type { RiskLevel } from '@/constants';

const { Text, Paragraph } = Typography;

/** 报告数据聚合结果 */
interface ReportBundle {
  /** 任务详情 */
  task: TaskDetailResponse | null;
  /** 风险等级（规则引擎终裁） */
  riskLevel: RiskLevel | null;
  /** 是否被阻塞 */
  isBlocked: boolean;
  /** 规则引擎是否已出结论 */
  ruleEvaluated: boolean;
  /** 结论文本 */
  conclusion: string;
  /** 风险项清单（仅规则引擎判定项） */
  riskItems: RiskItemResponse[];
  /** 报告 Markdown 原文（可能尚未生成） */
  reportContent: string;
}

export default function ReportPage(): React.ReactElement {
  const { taskNo = '' } = useParams<{ taskNo: string }>();
  const navigate = useNavigate();
  const { isMobile } = useResponsive();

  const [loading, setLoading] = useState(true);
  /** 无权限访问（后端 403）：展示专门的空态而非报错 */
  const [forbidden, setForbidden] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [bundle, setBundle] = useState<ReportBundle | null>(null);
  /** 下载中的格式，用于按钮 loading */
  const [downloading, setDownloading] = useState<string | null>(null);

  /**
   * 加载报告数据
   *
   * 设计说明：
   *   三个接口相互独立，用 allSettled 并行拉取，任一失败不影响其余部分渲染
   *   （例如报告尚未生成时，结论与风险项仍应正常展示）。
   */
  const loadReport = async (): Promise<void> => {
    if (!taskNo) return;
    setLoading(true);
    setForbidden(false);
    setErrorMsg(null);

    try {
      const [taskRes, reviewRes, reportRes] = await Promise.allSettled([
        taskApi.detail(taskNo),
        reviewApi.result(taskNo),
        reportApi.download(taskNo),
      ]);

      // 任务详情是页面骨架，失败则整体不可用
      if (taskRes.status === 'rejected') {
        const err = taskRes.reason;
        if (err instanceof ApiError && err.httpStatus === 403) {
          setForbidden(true);
        } else if (err instanceof ApiError && err.httpStatus === 404) {
          setErrorMsg(`任务不存在：${taskNo}`);
        } else {
          setErrorMsg(err instanceof Error ? err.message : '加载任务详情失败');
        }
        setLoading(false);
        return;
      }

      const review =
        reviewRes.status === 'fulfilled' ? reviewRes.value : null;
      const report =
        reportRes.status === 'fulfilled' ? reportRes.value : null;

      setBundle({
        task: taskRes.value,
        riskLevel: review?.risk_level ?? null,
        isBlocked: review?.is_blocked ?? false,
        ruleEvaluated: review?.rule_evaluated ?? false,
        conclusion: review?.conclusion_text ?? '',
        riskItems: review?.risk_items ?? [],
        reportContent: report?.content ?? '',
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // 无任务编号时不做任何请求：由下方的引导空态接管。
    // （此前直接 return 而不复位 loading，会让 /report 页面永久停在
    //   "正在加载审查报告…" 的转圈态，属于死页面。）
    if (!taskNo) {
      setLoading(false);
      return;
    }
    void loadReport();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskNo]);

  /** 按等级分组统计风险项数量 */
  const levelCount = useMemo(() => {
    const counts: Record<RiskLevel, number> = {
      low: 0,
      medium: 0,
      high: 0,
      critical: 0,
    };
    for (const item of bundle?.riskItems ?? []) {
      if (item.risk_level && counts[item.risk_level] !== undefined) {
        counts[item.risk_level] += 1;
      }
    }
    return counts;
  }, [bundle]);

  /** 风险项按等级从高到低排序（便于经办人先看最严重的） */
  const sortedRiskItems = useMemo(() => {
    return [...(bundle?.riskItems ?? [])].sort((a, b) => {
      const ra = RISK_LEVEL_ORDER[a.risk_level] ?? 0;
      const rb = RISK_LEVEL_ORDER[b.risk_level] ?? 0;
      if (rb !== ra) return rb - ra;
      return (a.risk_id || '').localeCompare(b.risk_id || '');
    });
  }, [bundle]);

  /** 下载报告文件 */
  const handleDownload = async (format: 'md' | 'pdf'): Promise<void> => {
    if (!taskNo) return;
    setDownloading(format);
    try {
      await reportApi.downloadFile(taskNo, format);
    } catch (error) {
      setErrorMsg(
        error instanceof Error ? error.message : `下载${format}失败`,
      );
    } finally {
      setDownloading(null);
    }
  };

  // ---------------- 缺少任务编号：引导前往大盘 ----------------
  // 说明：/report 不带 taskNo 时无法定位报告，给出明确引导而不是空转。
  if (!taskNo) {
    return (
      <Card>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div style={{ lineHeight: 1.9 }}>
              <div style={{ fontSize: 14, color: '#262626' }}>
                请先选择要查看的合同
              </div>
              <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                审查报告按合同逐份查看，请在合同审查大盘中点击「查看报告」进入
              </div>
            </div>
          }
        >
          <Button type="primary" onClick={() => navigate('/dashboard')}>
            前往合同审查大盘
          </Button>
        </Empty>
      </Card>
    );
  }

  // ---------------- 加载中 ----------------
  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '80px 0' }}>
        <Spin size="large" />
        <div style={{ marginTop: 12, color: '#8c8c8c', fontSize: 13 }}>
          正在加载审查报告…
        </div>
      </div>
    );
  }

  // ---------------- 无权限 ----------------
  if (forbidden) {
    return (
      <Card>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div style={{ lineHeight: 1.9 }}>
              <div style={{ fontSize: 14, color: '#262626' }}>
                你无权查看该任务的审查报告
              </div>
              <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                业务经办人仅可查看本人提交的合同报告
              </div>
            </div>
          }
        >
          <Button type="primary" onClick={() => navigate('/dashboard')}>
            返回合同审查大盘
          </Button>
        </Empty>
      </Card>
    );
  }

  // ---------------- 加载失败 ----------------
  if (errorMsg || !bundle?.task) {
    return (
      <Card>
        <Alert
          type="error"
          showIcon
          message="无法加载审查报告"
          description={errorMsg ?? '未知错误'}
          action={
            <Button size="small" onClick={() => void loadReport()}>
              重试
            </Button>
          }
        />
      </Card>
    );
  }

  const { task } = bundle;
  const hasRisk = sortedRiskItems.length > 0;

  return (
    <div>
      {/* ================= 顶部：返回 + 标题 + 下载 ================= */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 8,
            flexWrap: 'wrap',
          }}
        >
          <Space size={8} style={{ minWidth: 0 }}>
            <Button
              size="small"
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate('/dashboard')}
            >
              返回
            </Button>
            <Text strong style={{ fontSize: isMobile ? 14 : 15 }}>
              {task.contract_title || task.contract_no || task.task_no}
            </Text>
          </Space>
          <Space size={8} wrap>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadReport()}
            >
              刷新
            </Button>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              loading={downloading === 'md'}
              disabled={!bundle.reportContent}
              onClick={() => void handleDownload('md')}
            >
              Markdown
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<DownloadOutlined />}
              loading={downloading === 'pdf'}
              onClick={() => void handleDownload('pdf')}
            >
              PDF
            </Button>
          </Space>
        </div>
      </Card>

      {/* ================= 审查结论 ================= */}
      <Card
        size="small"
        title={
          <Space size={8}>
            <SafetyCertificateOutlined />
            <span>审查结论</span>
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        {bundle.isBlocked && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="该合同被规则引擎判定为阻塞"
            description={
              task.blocked_reason
                ? `原因：${task.blocked_reason}`
                : '存在必须人工处理的严重问题，请根据下方风险项修改后重新提交。'
            }
          />
        )}

        {!bundle.ruleEvaluated ? (
          <Alert
            type="info"
            showIcon
            message="审查尚未完成"
            description={`当前状态：${taskStatusLabel(task.status)}。审查完成后此处将展示规则引擎的最终结论。`}
          />
        ) : (
          <>
            {/* 结论摘要：等级 + 计数 */}
            <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
              <Col xs={12} sm={8} md={6}>
                <Statistic
                  title="最终风险等级"
                  value={bundle.riskLevel ? RISK_LEVEL_LABEL[bundle.riskLevel] : '—'}
                  valueStyle={{
                    fontSize: 18,
                    color: bundle.riskLevel
                      ? RISK_LEVEL_COLOR[bundle.riskLevel]
                      : '#8c8c8c',
                  }}
                />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Statistic
                  title="命中风险项"
                  value={sortedRiskItems.length}
                  suffix="项"
                  valueStyle={{ fontSize: 18 }}
                />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Statistic
                  title="严重 / 高风险"
                  value={levelCount.critical + levelCount.high}
                  suffix="项"
                  valueStyle={{
                    fontSize: 18,
                    color:
                      levelCount.critical + levelCount.high > 0
                        ? '#cf1322'
                        : undefined,
                  }}
                />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Statistic
                  title="任务状态"
                  value={taskStatusLabel(task.status)}
                  valueStyle={{ fontSize: 18 }}
                />
              </Col>
            </Row>

            {/* 结论文本 */}
            {bundle.conclusion && (
              <Paragraph
                style={{
                  marginBottom: 0,
                  fontSize: 13,
                  lineHeight: 1.9,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  color: '#262626',
                }}
              >
                {bundle.conclusion}
              </Paragraph>
            )}
          </>
        )}
      </Card>

      {/* ================= 任务信息 ================= */}
      <Card size="small" title="任务信息" style={{ marginBottom: 12 }}>
        <Descriptions
          column={isMobile ? 1 : 2}
          size="small"
          items={[
            { key: 'no', label: '任务编号', children: task.task_no },
            {
              key: 'title',
              label: '合同名称',
              children: task.contract_title || '—',
            },
            {
              key: 'status',
              label: '审查状态',
              children: taskStatusLabel(task.status),
            },
            {
              key: 'route',
              label: '审查路由',
              children: task.route_type || '—',
            },
            {
              key: 'created',
              label: '提交时间',
              children: task.created_at ? formatTime(task.created_at) : '—',
            },
            {
              key: 'writeback',
              label: '回写状态',
              children: task.write_back_status || '—',
            },
          ]}
        />
      </Card>

      {/* ================= 风险摘要 ================= */}
      <Card
        size="small"
        title={
          <Space size={8}>
            <FileTextOutlined />
            <span>风险摘要</span>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              共 {sortedRiskItems.length} 项
            </Text>
          </Space>
        }
      >
        {!hasRisk ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              bundle.ruleEvaluated
                ? '未命中任何风险规则'
                : '审查完成后此处展示风险清单'
            }
          />
        ) : (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {sortedRiskItems.map((item) => (
              <Card
                key={item.risk_id}
                size="small"
                styles={{ body: { padding: 12 } }}
                style={{
                  borderLeft: `3px solid ${
                    RISK_LEVEL_COLOR[item.risk_level] ?? '#d9d9d9'
                  }`,
                }}
              >
                {/* 标题行：等级 + 规则名 */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 8,
                    flexWrap: 'wrap',
                  }}
                >
                  <Space size={6} wrap>
                    <Tag
                      color={RISK_LEVEL_TAG_COLOR[item.risk_level]}
                      style={{ margin: 0, fontSize: 11 }}
                    >
                      {RISK_LEVEL_LABEL[item.risk_level]}
                    </Tag>
                    <Text strong style={{ fontSize: 13 }}>
                      {item.rule_name || item.rule_code || '未命名规则'}
                    </Text>
                  </Space>
                  {item.rule_code && (
                    <Text
                      type="secondary"
                      style={{ fontSize: 11, fontFamily: 'monospace' }}
                    >
                      {item.rule_code}
                    </Text>
                  )}
                </div>

                {/* 证据原文 */}
                {item.evidence_text && (
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      命中原文
                    </Text>
                    <div
                      style={{
                        marginTop: 2,
                        padding: '6px 8px',
                        background: '#fafafa',
                        borderRadius: 4,
                        fontSize: 12,
                        lineHeight: 1.8,
                        color: '#595959',
                        wordBreak: 'break-word',
                      }}
                    >
                      {item.evidence_text}
                    </div>
                  </div>
                )}

                {/* 修改建议 */}
                {item.suggestion_text && (
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      修改建议
                    </Text>
                    <div
                      style={{
                        marginTop: 2,
                        fontSize: 12,
                        lineHeight: 1.8,
                        color: '#262626',
                        wordBreak: 'break-word',
                      }}
                    >
                      {item.suggestion_text}
                    </div>
                  </div>
                )}

                {/* 法律依据 */}
                {item.legal_basis && (
                  <div style={{ marginTop: 6 }}>
                    <Tooltip title={item.legal_basis}>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        法律依据：{item.legal_basis}
                      </Text>
                    </Tooltip>
                  </div>
                )}
              </Card>
            ))}
          </Space>
        )}
      </Card>

      {/* ================= 只读声明 ================= */}
      <div
        style={{
          marginTop: 16,
          padding: '10px 12px',
          background: '#f6f8fa',
          border: '1px solid #e8e8e8',
          borderRadius: 8,
          fontSize: 12,
          lineHeight: 1.9,
          color: '#8c8c8c',
        }}
      >
        <div>
          本页为只读报告视图：审查结论与风险等级均由<Text strong>规则引擎</Text>终裁，
          大模型仅参与条款语义解析与建议草稿，不参与定级。
        </div>
        <div>
          如需对风险项进行复核、批注或回写审批意见，请由
          <Text strong>法务审查人</Text>在智能审查工作台中处理。
        </div>
        {task.status === TASK_STATUS.BLOCKED && (
          <div style={{ marginTop: 4 }}>
            当前合同处于阻塞状态，可在
            <Text strong>合同审查大盘</Text>中点击「重新上传」提交修正版。
          </div>
        )}
      </div>
    </div>
  );
}
