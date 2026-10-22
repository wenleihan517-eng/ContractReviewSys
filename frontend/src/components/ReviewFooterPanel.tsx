// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/components/ReviewFooterPanel.tsx
/**
 * 底部：协同回写区
 *
 * 【文件职责】
 *   承载审查完成后的收口动作：
 *     1. 综合风险摘要（等级 + 分布 + 判定口径说明）
 *     2. 审批关注要点（从高等级风险提炼）
 *     3. 法务人工批注输入框
 *     4. 评论预览与一键回写审批单
 *     5. 审查报告导出预览
 *
 * 【权责红线（本文件最关键的设计约束）】
 *   回写只允许写"评论"。审批动作（通过/驳回/转办/撤回/加签）
 *   一律不在此处提供任何入口。
 *
 *   后端 approval_guard 采用默认拒绝原则：白名单只有 /comments 与
 *   /comments/batch，其余路径全部拦截。前端主动不提供动作入口，
 *   与后端形成双重防线——即使有人绕过 UI 直接调用接口，后端也会拒绝。
 *
 *   因此本组件中不存在任何"审批通过""驳回"按钮，这不是遗漏，
 *   而是刻意的架构约束。若后续业务确实需要审批动作，必须走独立的
 *   审批系统接口，不能挂载在智能审查的能力边界内。
 *
 * 【回写前置校验】
 *   回写前先做评论预览，预览结果包含后端格式校验结论。
 *   校验未通过时禁止回写并明确提示问题项——
 *   避免把格式不合规的评论写入审批单，
 *   事后清理审批单评论的成本远高于事前拦一道。
 */

import { useState } from 'react';
import {
  Alert,
  App,
  Button,
  Col,
  Descriptions,
  Drawer,
  Divider,
  Input,
  Modal,
  Row,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  CloudUploadOutlined,
  DownloadOutlined,
  EyeOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';

import {
  RISK_LEVEL_COLOR,
  RISK_LEVEL_DESCRIPTION,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_TAG_COLOR,
  THEME,
  WRITE_BACK_STATUS_COLOR,
  WRITE_BACK_STATUS_LABEL,
  type RiskLevel,
  type WriteBackStatus,
} from '@/constants';
import { downloadFile, formatTime } from '@/utils';
import { ApiError } from '@/api/client';
import { reportApi } from '@/api/endpoints';
import type {
  LocatedRisk,
  ReportFormat,
  TaskDetailResponse,
} from '@/types';

const { Text, Paragraph, Title } = Typography;
const { TextArea } = Input;

/**
 * 报告 Markdown 渲染的组件映射：用 antd 排版组件承载标题/段落/列表，
 * 不依赖全局样式覆盖（遵循 UX_UI §6.3），表格等由 remark-gfm 渲染。
 */
const markdownComponents: Components = {
  h1: (props) => {
    const { node: _node, ...rest } = props;
    return <Title level={3} style={{ marginTop: 8, marginBottom: 8 }} {...rest} />;
  },
  h2: (props) => {
    const { node: _node, ...rest } = props;
    return <Title level={4} style={{ marginTop: 12, marginBottom: 6 }} {...rest} />;
  },
  h3: (props) => {
    const { node: _node, ...rest } = props;
    return <Title level={5} style={{ marginTop: 10, marginBottom: 4 }} {...rest} />;
  },
  p: (props) => {
    const { node: _node, ...rest } = props;
    return <Paragraph style={{ fontSize: 13, lineHeight: 1.9, marginBottom: 8 }} {...rest} />;
  },
  li: (props) => {
    const { node: _node, ...rest } = props;
    return <li style={{ fontSize: 13, lineHeight: 1.9 }} {...rest} />;
  },
};

interface ReviewFooterPanelProps {
  /** 任务详情 */
  taskDetail: TaskDetailResponse | null;
  /** 综合风险等级 */
  overallLevel: RiskLevel | null;
  /** 风险等级分布 */
  distribution: Record<RiskLevel, number>;
  /** 带定位信息的风险项 */
  locatedRisks: LocatedRisk[];
  /** 审批关注要点 */
  approvalHighlights: string[];
  /** 法务批注文本 */
  legalComment: string;
  /** 批注变更回调 */
  onCommentChange: (text: string) => void;
  /** 评论预览结果 */
  commentPreview: { comment_text: string; validation_passed: boolean; validation_issues: string[]; api_path: string } | null;
  /** 报告 Markdown 原文 */
  reportContent: string;
  /** 报告是否已生成 */
  reportGenerated: boolean;
  /** 各项操作的进行中标记 */
  submitting: {
    preview: boolean;
    writeBack: boolean;
    report: boolean;
  };
  /** 预览评论 */
  onPreview: () => Promise<void>;
  /** 回写评论 */
  onWriteBack: (preview: boolean) => Promise<boolean>;
  /** 生成报告 */
  onGenerateReport: () => Promise<void>;
}

export default function ReviewFooterPanel({
  taskDetail,
  overallLevel,
  distribution,
  locatedRisks,
  approvalHighlights,
  legalComment,
  onCommentChange,
  commentPreview,
  reportContent,
  reportGenerated,
  submitting,
  onPreview,
  onWriteBack,
  onGenerateReport,
}: ReviewFooterPanelProps): React.ReactElement {
  const { message } = App.useApp();

  /** 报告预览抽屉可见性 */
  const [reportDrawerOpen, setReportDrawerOpen] = useState(false);
  /** 评论预览弹窗可见性 */
  const [commentModalOpen, setCommentModalOpen] = useState(false);

  /** 正式风险项总数（排除参考项） */
  const officialCount = locatedRisks.filter(
    (located) => !located.item.reference_only,
  ).length;
  /** 参考项总数 */
  const referenceCount = locatedRisks.filter(
    (located) => located.item.reference_only,
  ).length;

  const writeBackStatus: WriteBackStatus =
    taskDetail?.write_back_status ?? 'not_written';

  /** 是否存在高风险及以上（决定是否允许回写） */
  const hasBlockingRisk =
    overallLevel === 'critical' || overallLevel === 'high';

  /**
   * 打开评论预览
   *
   * 说明：预览同时完成两件事——获取最终评论文本、拿到后端格式校验结果。
   * 使用者可以在弹窗中确认文本后再决定是否回写。
   */
  const handleOpenPreview = (): void => {
    void (async () => {
      await onPreview();
      setCommentModalOpen(true);
    })();
  };

  /**
   * 执行回写
   *
   * 参数：
   *   isPreview: true 表示演练（不落库），false 表示正式回写
   */
  const handleWriteBack = (isPreview: boolean): void => {
    if (!isPreview && commentPreview && !commentPreview.validation_passed) {
      message.error('评论格式校验未通过，请先处理提示的问题项');
      return;
    }

    void (async () => {
      try {
        const succeeded = await onWriteBack(isPreview);
        if (isPreview) {
          message.success('演练完成，未写入审批单');
        } else if (succeeded) {
          message.success('审查意见已回写至审批单评论区');
          setCommentModalOpen(false);
        } else {
          message.warning('回写未成功，请查看返回状态后重试');
        }
      } catch {
        message.error('回写失败，请稍后重试或检查审批系统连通性');
      }
    })();
  };

  /** 生成报告 */
  const handleGenerateReport = (): void => {
    void (async () => {
      try {
        await onGenerateReport();
        setReportDrawerOpen(true);
        message.success('审查报告已生成');
      } catch {
        message.error('报告生成失败，请稍后重试');
      }
    })();
  };

  /** 下载报告：Markdown 走前端本地落盘，PDF 走后端流式接口 */
  const handleDownloadReport = (format: ReportFormat): void => {
    // Markdown 内容已在抽屉中加载，直接本地生成文件，无需再请求后端
    if (format === 'md') {
      if (!reportContent) {
        message.warning('报告内容尚未加载，请先生成报告');
        return;
      }
      const fileName = `审查报告_${taskDetail?.contract_title ?? '合同'}_${taskDetail?.task_no ?? ''}.md`;
      downloadFile(reportContent, fileName);
      message.success('报告已开始下载');
      return;
    }

    // PDF 为二进制流，必须由后端渲染后下发（前端不做 PDF 合成）
    // 用 task_id 定位任务文档；taskDetail 尚未加载时给出提示而非静默失败
    const taskId = taskDetail?.task_id;
    if (!taskId) {
      message.warning('任务信息尚未加载，暂时无法下载 PDF');
      return;
    }
    void (async () => {
      try {
        await reportApi.downloadFile(taskId, 'pdf');
        message.success('PDF 报告已开始下载');
      } catch (err) {
        // 后端在 PDF 未生成 / 功能未开启时返回 400 或 404，
        // 文案优先透出后端 message，便于用户判断是否需要先生成
        const tip =
          err instanceof ApiError ? err.message : 'PDF 下载失败，请稍后重试';
        message.error(tip);
      }
    })();
  };

  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {/* ================= 综合风险摘要 ================= */}
        <Row gutter={12}>
          <Col span={6}>
            <div
              style={{
                padding: '10px 12px',
                background: THEME.SURFACE_SECONDARY,
                borderRadius: 6,
                border: `1px solid ${THEME.BORDER_SECONDARY}`,
                height: '100%',
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                综合风险等级
              </Text>
              <div style={{ marginTop: 4 }}>
                {overallLevel ? (
                  <Tag
                    color={RISK_LEVEL_TAG_COLOR[overallLevel]}
                    style={{ margin: 0, fontSize: 14, padding: '2px 8px', fontWeight: 600 }}
                  >
                    {RISK_LEVEL_LABEL[overallLevel]}
                  </Tag>
                ) : (
                  <Tag style={{ margin: 0 }}>未判定</Tag>
                )}
              </div>
              {overallLevel && (
                <div
                  style={{
                    fontSize: 11,
                    color: RISK_LEVEL_COLOR[overallLevel],
                    marginTop: 4,
                    lineHeight: 1.5,
                  }}
                >
                  {RISK_LEVEL_DESCRIPTION[overallLevel]}
                </div>
              )}
            </div>
          </Col>

          <Col span={6}>
            <div
              style={{
                padding: '10px 12px',
                background: THEME.SURFACE_SECONDARY,
                borderRadius: 6,
                border: `1px solid ${THEME.BORDER_SECONDARY}`,
                height: '100%',
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                风险项统计
              </Text>
              <div style={{ marginTop: 2 }}>
                <Statistic
                  value={officialCount}
                  suffix="项"
                  valueStyle={{ fontSize: 20 }}
                />
              </div>
              <Text type="secondary" style={{ fontSize: 11 }}>
                参考项 {referenceCount} 项（不参与定级）
              </Text>
            </div>
          </Col>

          {(['critical', 'high'] as RiskLevel[]).map((level) => (
            <Col span={6} key={level}>
              <div
                style={{
                  padding: '10px 12px',
                  background: THEME.SURFACE_SECONDARY,
                  borderRadius: 6,
                  border: `1px solid ${THEME.BORDER_SECONDARY}`,
                  height: '100%',
                }}
              >
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {RISK_LEVEL_LABEL[level]}
                </Text>
                <div style={{ marginTop: 2 }}>
                  <Statistic
                    value={distribution[level] ?? 0}
                    suffix="项"
                    valueStyle={{
                      fontSize: 20,
                      color:
                        distribution[level] > 0
                          ? RISK_LEVEL_COLOR[level]
                          : undefined,
                    }}
                  />
                </div>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {RISK_LEVEL_DESCRIPTION[level]}
                </Text>
              </div>
            </Col>
          ))}
        </Row>

        {/* ================= 审批关注要点 ================= */}
        {approvalHighlights.length > 0 && (
          <div
            style={{
              padding: '10px 12px',
              background: THEME.WARNING_SURFACE,
              borderRadius: 6,
              border: `1px solid ${THEME.WARNING_BORDER}`,
            }}
          >
            <Space size={6} style={{ marginBottom: 6 }}>
              <SafetyCertificateOutlined style={{ color: '#fa8c16' }} />
              <Text strong style={{ fontSize: 13 }}>
                审批关注要点
              </Text>
              <Tooltip title="由规则引擎判定为高风险及以上的风险项提炼，可直接引用至审批意见">
                <Text type="secondary" style={{ fontSize: 11 }}>
                  （规则引擎判定 · 共 {approvalHighlights.length} 条）
                </Text>
              </Tooltip>
            </Space>
            <div style={{ fontSize: 12, lineHeight: 1.9, color: '#614700' }}>
              {approvalHighlights.map((item, index) => (
                <div key={`${item}-${index}`}>
                  {index + 1}. {item}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ================= 高风险阻断提示 ================= */}
        {hasBlockingRisk && (
          <Alert
            type="warning"
            showIcon
            message="存在高风险及以上条款，建议完成整改后再提交审批"
            description={
              <span style={{ fontSize: 12 }}>
                规则引擎已将本次审查判定为需整改。
                您仍可回写审查意见至审批单评论区，但审批结论由审批系统按流程决定，
                智能审查不代为决策。
              </span>
            }
          />
        )}

        {/* ================= 法务批注与回写 ================= */}
        <Row gutter={12}>
          {/* ---------------- 左：人工批注 ---------------- */}
          <Col span={14}>
            <div style={{ marginBottom: 6 }}>
              <Space size={6}>
                <Text strong style={{ fontSize: 13 }}>
                  法务人工批注
                </Text>
                <Tooltip title="批注将与系统审查意见一并回写至审批单评论区，不改变审批结论与风险定级">
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    （将随审查意见一并回写，不改变判定结论）
                  </Text>
                </Tooltip>
              </Space>
            </div>
            <TextArea
              value={legalComment}
              onChange={(event) => onCommentChange(event.target.value)}
              placeholder="请填写法务复核意见，例如：第 3.2 条付款条款已与业务部门确认，同意在补充验收单后放行；其余条款按系统建议修订后重新提交。"
              autoSize={{ minRows: 4, maxRows: 8 }}
              style={{ fontSize: 13 }}
              showCount
              maxLength={2000}
            />
          </Col>

          {/* ---------------- 右：回写状态与操作 ---------------- */}
          <Col span={10}>
            <div style={{ marginBottom: 6 }}>
              <Text strong style={{ fontSize: 13 }}>
                回写与导出
              </Text>
            </div>

            <Descriptions
              size="small"
              column={1}
              colon={false}
              style={{ marginBottom: 10 }}
              items={[
                {
                  key: 'status',
                  label: <Text type="secondary" style={{ fontSize: 12 }}>回写状态</Text>,
                  children: (
                    <Tag
                      color={WRITE_BACK_STATUS_COLOR[writeBackStatus]}
                      style={{ margin: 0 }}
                    >
                      {WRITE_BACK_STATUS_LABEL[writeBackStatus]}
                    </Tag>
                  ),
                },
                {
                  key: 'taskNo',
                  label: <Text type="secondary" style={{ fontSize: 12 }}>任务编号</Text>,
                  children: (
                    <Text style={{ fontSize: 12 }}>{taskDetail?.task_no ?? '—'}</Text>
                  ),
                },
                {
                  key: 'updatedAt',
                  label: <Text type="secondary" style={{ fontSize: 12 }}>最近更新</Text>,
                  children: (
                    <Text style={{ fontSize: 12 }}>
                      {formatTime(taskDetail?.updated_at ?? null, 'MM-DD HH:mm:ss')}
                    </Text>
                  ),
                },
              ]}
            />

            <Space direction="vertical" style={{ width: '100%' }} size={8}>
              <Button
                block
                icon={<EyeOutlined />}
                loading={submitting.preview}
                onClick={handleOpenPreview}
              >
                预览审查意见
              </Button>

              <Button
                block
                type="primary"
                icon={<CloudUploadOutlined />}
                loading={submitting.writeBack}
                onClick={() => handleWriteBack(false)}
              >
                一键回写审批单
              </Button>

              <Space.Compact block>
                <Button
                  icon={<ReloadOutlined />}
                  loading={submitting.report}
                  onClick={handleGenerateReport}
                  style={{ width: '55%' }}
                >
                  生成报告
                </Button>
                <Button
                  icon={<DownloadOutlined />}
                  disabled={!reportGenerated}
                  onClick={() => setReportDrawerOpen(true)}
                  style={{ width: '45%' }}
                >
                  报告预览
                </Button>
              </Space.Compact>

              <Text type="secondary" style={{ fontSize: 11, lineHeight: 1.6 }}>
                回写仅写入审批单评论区。智能审查不提供审批动作入口，
                审批结论由审批系统按流程判定。
              </Text>
            </Space>
          </Col>
        </Row>
      </div>

      {/* ================= 评论预览弹窗 ================= */}
      <Modal
        title="审查意见预览"
        open={commentModalOpen}
        width={720}
        onCancel={() => setCommentModalOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setCommentModalOpen(false)}>关闭</Button>
            <Button onClick={() => handleWriteBack(true)} loading={submitting.writeBack}>
              演练（不落库）
            </Button>
            <Button
              type="primary"
              icon={<CloudUploadOutlined />}
              disabled={commentPreview ? !commentPreview.validation_passed : false}
              loading={submitting.writeBack}
              onClick={() => handleWriteBack(false)}
            >
              确认回写
            </Button>
          </Space>
        }
      >
        {commentPreview ? (
          <>
            {/* 格式校验结果 */}
            {commentPreview.validation_passed ? (
              <Alert
                type="success"
                showIcon
                icon={<CheckCircleOutlined />}
                message="评论格式校验通过"
                style={{ marginBottom: 12 }}
              />
            ) : (
              <Alert
                type="error"
                showIcon
                message="评论格式校验未通过"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                    {commentPreview.validation_issues.map((issue, index) => (
                      <li key={`${issue}-${index}`}>{issue}</li>
                    ))}
                  </ul>
                }
                style={{ marginBottom: 12 }}
              />
            )}

            {/* 回写目标 */}
            <div style={{ marginBottom: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                回写目标接口：
              </Text>
              <Text code style={{ fontSize: 12 }}>
                {commentPreview.api_path}
              </Text>
              <Tooltip title="审批接口白名单：仅评论类接口允许写入，动作类接口（审批/驳回/转办等）一律拒绝">
                <Tag color="green" style={{ marginLeft: 8, fontSize: 11 }}>
                  白名单内
                </Tag>
              </Tooltip>
            </div>

            <Divider style={{ margin: '8px 0' }} />

            {/* 评论正文 */}
            <Text type="secondary" style={{ fontSize: 12 }}>
              最终写入的评论内容：
            </Text>
            <Paragraph
              style={{
                marginTop: 6,
                padding: 12,
                background: THEME.SURFACE_SECONDARY,
                border: `1px solid ${THEME.BORDER_SECONDARY}`,
                borderRadius: 4,
                fontSize: 12,
                lineHeight: 1.9,
                whiteSpace: 'pre-wrap',
                maxHeight: 320,
                overflow: 'auto',
              }}
            >
              {commentPreview.comment_text}
            </Paragraph>

            {legalComment.trim() && (
              <>
                <Divider style={{ margin: '8px 0' }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    法务补充批注
                  </Text>
                </Divider>
                <Paragraph
                  style={{
                    padding: 12,
                    background: '#f6ffed',
                    border: '1px solid #b7eb8f',
                    borderRadius: 4,
                    fontSize: 12,
                    lineHeight: 1.9,
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {legalComment}
                </Paragraph>
              </>
            )}
          </>
        ) : (
          <Text type="secondary">正在获取评论预览…</Text>
        )}
      </Modal>

      {/* ================= 报告预览抽屉 ================= */}
      <Drawer
        title={
          <Space size={8}>
            <FileTextOutlined />
            <span>审查报告预览</span>
            {reportContent && (
              <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                {reportContent.length} 字符
              </Text>
            )}
          </Space>
        }
        placement="right"
        width={760}
        open={reportDrawerOpen}
        onClose={() => setReportDrawerOpen(false)}
        extra={
          <Space size={8}>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              disabled={!reportContent}
              onClick={() => handleDownloadReport('md')}
            >
              下载 Markdown
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<DownloadOutlined />}
              disabled={!reportContent}
              onClick={() => handleDownloadReport('pdf')}
            >
              下载 PDF
            </Button>
          </Space>
        }
      >
        {reportContent ? (
          <div
            style={{
              fontSize: 13,
              lineHeight: 1.9,
              wordBreak: 'break-word',
              color: THEME.TEXT_PRIMARY,
            }}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {reportContent}
            </ReactMarkdown>
          </div>
        ) : (
          <div style={{ textAlign: 'center', paddingTop: 60 }}>
            <Title level={5} type="secondary">
              报告内容尚未生成
            </Title>
            <Text type="secondary" style={{ fontSize: 12 }}>
              请先点击"生成报告"，生成后此处展示完整报告内容，支持下载为 Markdown 文件。
            </Text>
            <div style={{ marginTop: 16 }}>
              <Button
                type="primary"
                loading={submitting.report}
                onClick={handleGenerateReport}
              >
                立即生成报告
              </Button>
            </div>
          </div>
        )}
      </Drawer>
    </>
  );
}
