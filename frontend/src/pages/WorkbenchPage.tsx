// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/WorkbenchPage.tsx
/**
 * 页面二：智能审查工作台（核心页面）
 *
 * 【页面职责】
 *   单份合同的深度复核作业面，采用"双栏 + 底栏"的三段式布局：
 *
 *     左栏：文档视窗
 *       - PDF / Word 在线预览，分页查看
 *       - 风险条款高亮，支持页码跳转
 *       - 保存原文字符位置与段落索引，与右栏联动
 *
 *     右栏：审查结果与建议区
 *       - 风险卡片按高/中/低排序
 *       - 卡片含风险描述、法律依据、原文片段、AI 推荐修改条款
 *       - 支持一键复制采纳修改建议
 *
 *     底栏：协同回写区
 *       - 综合风险摘要、审批关注要点
 *       - 法务人工批注输入框
 *       - 一键回写审批单按钮、报告导出预览
 *
 * 【左右栏联动机制】
 *   右栏点击卡片 → selectRisk(riskId) → store 派生出 targetPage 与
 *   highlightKeyword → 左栏 useEffect 监听后跳页并滚动到命中段落。
 *   反向：左栏点击高亮文本 → onSelectRisk(riskId) → 右栏卡片高亮并滚动到视野内。
 *
 *   联动的坐标系是"全文字符区间"，不是文本匹配。
 *   这保证了同一段文字在合同中多处出现时不会跳错位置。
 *
 * 【无任务编号时的引导】
 *   路由 /workbench 不带 taskNo 时，展示任务选择引导而非空白页——
 *   使用者可能是从书签或直接输入地址进入的。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Progress,
  Segmented,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import {
  ArrowLeftOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons';

import DocumentViewer from '@/components/DocumentViewer';
import RiskCard from '@/components/RiskCard';
import ReviewFooterPanel from '@/components/ReviewFooterPanel';
import { taskApi } from '@/api/endpoints';
import {
  BLOCKED_REASON_LABEL,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_TAG_COLOR,
  FAILURE_TYPE_LABEL,
  TASK_STATUS,
  TASK_STATUS_IN_PROGRESS,
  WRITE_BACK_STATUS_COLOR,
  WRITE_BACK_STATUS_LABEL,
  WORKBENCH_PANEL_OFFSET_PX,
  type RiskLevel,
} from '@/constants';
import {
  selectApprovalHighlights,
  selectFilteredRisks,
  selectOverallRiskLevel,
  useWorkbenchStore,
} from '@/stores/workbenchStore';
import { useTaskStore } from '@/stores/taskStore';
import { useAuthStore } from '@/stores/authStore';
import { formatTime, taskStatusLabel } from '@/utils';
import { useResponsive } from '@/hooks/useResponsive';

const { Text, Title } = Typography;

export default function WorkbenchPage(): React.ReactElement {
  const { taskNo } = useParams<{ taskNo: string }>();
  const navigate = useNavigate();
  const { message, modal } = App.useApp();
  // 大盘/工作台共用的任务操作（重试 / 重新上传）与权限来源
  const retryTask = useTaskStore((state) => state.retryTask);
  const replaceFile = useTaskStore((state) => state.replaceFile);
  const user = useAuthStore((state) => state.user);

  // ---------------- Store 状态 ----------------
  const taskDetail = useWorkbenchStore((state) => state.taskDetail);
  const reviewResult = useWorkbenchStore((state) => state.reviewResult);  const pages = useWorkbenchStore((state) => state.pages);
  const locatedRisks = useWorkbenchStore((state) => state.locatedRisks);
  const riskDistribution = useWorkbenchStore((state) => state.riskDistribution);
  const selectedRiskId = useWorkbenchStore((state) => state.selectedRiskId);
  const targetPage = useWorkbenchStore((state) => state.targetPage);
  const highlightKeyword = useWorkbenchStore((state) => state.highlightKeyword);
  const levelFilter = useWorkbenchStore((state) => state.levelFilter);
  const legalComment = useWorkbenchStore((state) => state.legalComment);
  const commentPreview = useWorkbenchStore((state) => state.commentPreview);
  const reportContent = useWorkbenchStore((state) => state.reportContent);
  const reportResult = useWorkbenchStore((state) => state.reportResult);
  const loading = useWorkbenchStore((state) => state.loading);
  const submitting = useWorkbenchStore((state) => state.submitting);
  const error = useWorkbenchStore((state) => state.error);

  const initTask = useWorkbenchStore((state) => state.initTask);
  const refreshProgress = useWorkbenchStore((state) => state.refreshProgress);
  const selectRisk = useWorkbenchStore((state) => state.selectRisk);
  const selectRiskByPosition = useWorkbenchStore(
    (state) => state.selectRiskByPosition,
  );
  const setTargetPage = useWorkbenchStore((state) => state.setTargetPage);
  const setLevelFilter = useWorkbenchStore((state) => state.setLevelFilter);
  const saveSuggestion = useWorkbenchStore((state) => state.saveSuggestion);
  const adoptRecommend = useWorkbenchStore((state) => state.adoptRecommend);
  const setLegalComment = useWorkbenchStore((state) => state.setLegalComment);
  const previewComment = useWorkbenchStore((state) => state.previewComment);
  const writeBackComment = useWorkbenchStore((state) => state.writeBackComment);
  const generateReport = useWorkbenchStore((state) => state.generateReport);
  const reset = useWorkbenchStore((state) => state.reset);
  const stopPolling = useWorkbenchStore((state) => state.stopPolling);

  /** 当前视口是否处于手机尺寸（< md） */
  const { isMobile } = useResponsive();

  /** 右栏风险卡片容器（用于选中时滚动到视野内） */
  const riskListRef = useRef<HTMLDivElement | null>(null);
  /** 手动重跑进行中 */
  const [retrying, setRetrying] = useState(false);

  // ====================================================================
  // 一、初始化
  // ====================================================================

  useEffect(() => {
    if (!taskNo) return;

    // 切换任务时先重置，避免上一个任务的数据短暂串到新任务上
    reset();
    void initTask(taskNo);

    return () => stopPolling();
  }, [taskNo, initTask, reset, stopPolling]);

  // ====================================================================
  // 二、联动：选中卡片后滚动到右栏可见位置
  // ====================================================================

  useEffect(() => {
    if (!selectedRiskId || !riskListRef.current) return;

    const container = riskListRef.current;
    const node = container.querySelector<HTMLElement>(
      `[data-risk-card="${CSS.escape(selectedRiskId)}"]`,
    );
    if (!node) return;

    node.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [selectedRiskId]);

  // ====================================================================
  // 三、派生数据
  // ====================================================================

  /** 综合风险等级（优先取后端规则引擎结论，缺失时按风险项兜底计算） */
  const overallLevel = useMemo(
    () =>
      selectOverallRiskLevel(
        reviewResult,
        locatedRisks.map((located) => located.item),
      ),
    [reviewResult, locatedRisks],
  );

  /** 审批关注要点 */
  const approvalHighlights = useMemo(
    () => selectApprovalHighlights(locatedRisks),
    [locatedRisks],
  );

  /** 按等级筛选后的风险卡片 */
  const filteredRisks = useMemo(
    () => selectFilteredRisks(locatedRisks, levelFilter),
    [locatedRisks, levelFilter],
  );

  /** 等级筛选选项（带数量，方便判断哪一档有内容） */
  const levelFilterOptions = useMemo(
    () =>
      (['critical', 'high', 'medium', 'low'] as RiskLevel[]).map((level) => ({
        label: `${RISK_LEVEL_LABEL[level]} ${riskDistribution[level] ?? 0}`,
        value: level,
      })),
    [riskDistribution],
  );

  /** 是否为进行中的任务 */
  const isInProgress = taskDetail
    ? TASK_STATUS_IN_PROGRESS.includes(taskDetail.status)
    : false;

  /** 是否被阻塞 */
  const isBlocked = taskDetail?.status === TASK_STATUS.BLOCKED;

  /** 重新上传权限：仅提交经办人 / 管理员（权责分离，2026-09-20） */
  const canReplaceFile =
    (taskDetail?.status === TASK_STATUS.FAILED ||
      taskDetail?.status === TASK_STATUS.BLOCKED) &&
    (user?.role === 'admin' ||
      (taskDetail.created_by && taskDetail.created_by === user?.username));

  /** 失败任务一键重试（复用大盘 taskStore 能力） */
  const handleWorkbenchRetry = (): void => {
    if (!taskDetail?.task_no) return;
    void (async () => {
      try {
        await retryTask(taskDetail.task_no);
        message.success('任务已重新排队，正在重试');
        void initTask(taskDetail.task_no);
      } catch {
        message.error('重试失败，请稍后重试');
      }
    })();
  };

  /** 失败任务重新上传替换文件 */
  const handleWorkbenchReplace = (file: File): void => {
    if (!taskDetail?.task_no) return;
    void (async () => {
      try {
        await replaceFile(taskDetail.task_no, file);
        message.success('合同文件已替换并重新提交审查');
        void initTask(taskDetail.task_no);
      } catch (e) {
        message.error(e instanceof Error ? e.message : '重新上传失败');
      }
    })();
  };

  // ====================================================================
  // 四、交互处理
  // ====================================================================

  /**
   * 处理左栏页码变化
   *
   * 说明：用户在左栏手动翻页时同步 store 的 targetPage，
   * 避免右栏再次点击同一风险卡片时因为 targetPage 未变而不触发跳页。
   */
  const handlePageChange = useCallback(
    (page: number): void => {
      if (page !== targetPage) setTargetPage(page);
    },
    [targetPage, setTargetPage],
  );

  /** 手动刷新 */
  const handleRefresh = (): void => {
    void (async () => {
      await refreshProgress(false);
      message.success('已刷新最新状态');
    })();
  };

  /** 重跑阻塞任务 */
  const handleRetryBlocked = (): void => {
    if (!taskNo) return;

    modal.confirm({
      title: '确认重跑该阻塞任务？',
      icon: <ExclamationCircleOutlined />,
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          <div>
            阻塞原因：
            <Text type="danger">
              {taskDetail?.blocked_reason ?? '未标注'}
            </Text>
          </div>
          {taskDetail?.blocked_detail && (
            <div style={{ color: '#8c8c8c', marginTop: 4 }}>
              {taskDetail.blocked_detail}
            </div>
          )}
          <div style={{ marginTop: 8, color: '#8c8c8c' }}>
            已重试 {taskDetail?.retry_count ?? 0} / {taskDetail?.max_retry ?? 3} 次。
            若阻塞原因为文档本身问题，重跑仍会再次阻塞。
          </div>
        </div>
      ),
      okText: '确认重跑',
      cancelText: '取消',
      onOk: async () => {
        setRetrying(true);
        try {
          await taskApi.retry(taskNo);
          await refreshProgress(false);
          message.success('任务已重新排队');
        } catch {
          message.error('重跑失败，请稍后重试');
        } finally {
          setRetrying(false);
        }
      },
    });
  };

  /** 无任务编号时的引导页 */
  if (!taskNo) {
    return (
      <Card>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div>
              <Title level={5} style={{ marginBottom: 4 }}>
                请先选择要审查的合同
              </Title>
              <Text type="secondary" style={{ fontSize: 13 }}>
                从审查大盘中点击任一合同的「审查」进入工作台
              </Text>
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

  // ====================================================================
  // 五、主渲染
  // ====================================================================

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* ================= 顶部：失败错误横幅（2026-09-20 交互优化） ================= */}
      {taskDetail?.status === TASK_STATUS.FAILED && (
        <Alert
          type="error"
          showIcon
          message="合同审查执行失败"
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <span>
                失败类型：
                {FAILURE_TYPE_LABEL[taskDetail.failure_type ?? 'other'] ??
                  taskDetail.failure_type}
                ，失败原因：{taskDetail.failure_reason ?? '未知'}
              </span>
              <span style={{ color: '#8c8c8c' }}>
                瞬时异常可直接一键重试；文件损坏请重新上传替换合同文件后重新提交。
              </span>
            </Space>
          }
          action={
            <Space>
              <Button size="small" icon={<SyncOutlined />} onClick={() => handleWorkbenchRetry()}>
                一键重试
              </Button>
              {canReplaceFile && (
                <Upload
                  showUploadList={false}
                  accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp"
                  beforeUpload={(file) => {
                    void handleWorkbenchReplace(file as unknown as File);
                    return false;
                  }}
                >
                  <Button size="small">重新上传</Button>
                </Upload>
              )}
            </Space>
          }
        />
      )}

      {/* ================= 顶部：审查完成成功提示条（2026-09-20） ================= */}
      {taskDetail?.status === TASK_STATUS.COMPLETED && (
        <Alert
          type="success"
          showIcon
          message="审查已完成"
          description={`共命中 ${locatedRisks.length} 项风险`}
          action={
            <Button size="small" onClick={() => void generateReport('md')}>
              查看风险报告
            </Button>
          }
        />
      )}

      {/* ================= 顶部：已阻塞操作横幅（2026-09-20） ================= */}
      {isBlocked && (
        <Alert
          type="warning"
          showIcon
          message="任务已被规则引擎阻塞"
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <span>
                阻塞原因：
                {taskDetail?.blocked_reason
                  ? BLOCKED_REASON_LABEL[taskDetail.blocked_reason] ??
                    taskDetail.blocked_reason
                  : '未标注'}
                。重跑将从阻塞阶段继续（内容缺陷类阻塞重跑仍会再次阻塞）；
                若为文件内容问题，建议重新上传替换修正版合同文件。
              </span>
            </Space>
          }
          action={
            <Space>
              <Button size="small" icon={<SyncOutlined />} onClick={() => handleWorkbenchRetry()}>
                重跑
              </Button>
              {canReplaceFile && (
                <Upload
                  showUploadList={false}
                  accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp"
                  beforeUpload={(file) => {
                    void handleWorkbenchReplace(file as unknown as File);
                    return false;
                  }}
                >
                  <Button size="small">重新上传</Button>
                </Upload>
              )}
            </Space>
          }
        />
      )}

      {/* ================= 顶部：任务概要条 ================= */}
      <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
        <Space
          style={{ width: '100%', justifyContent: 'space-between' }}
          align="start"
          wrap
        >
          <Space size={12} align="start" wrap>
            <Button
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate('/dashboard')}
            >
              返回大盘
            </Button>

            <div>
              <Space size={8} wrap>
                <Text strong style={{ fontSize: 15 }}>
                  {taskDetail?.contract_title ?? '加载中…'}
                </Text>
              </Space>

              <Space size={8} style={{ marginTop: 4 }} wrap>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {taskDetail?.contract_no || taskDetail?.task_no || '—'}
                </Text>
                {overallLevel && (
                  <Tag
                    color={RISK_LEVEL_TAG_COLOR[overallLevel]}
                    style={{ margin: 0, fontWeight: 600 }}
                  >
                    {RISK_LEVEL_LABEL[overallLevel]}
                  </Tag>
                )}
                <Tooltip title="规则引擎终裁：风险等级与审查结论由规则引擎产出，大模型仅提供建议草稿">
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    由规则引擎定级
                  </Text>
                </Tooltip>
                {taskDetail && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    创建于 {formatTime(taskDetail.created_at, 'YYYY-MM-DD HH:mm')}
                  </Text>
                )}
              </Space>
            </div>
          </Space>

          <Space size={8} wrap>
            {taskDetail && (
              <>
                <Tag
                  color={WRITE_BACK_STATUS_COLOR[taskDetail.write_back_status]}
                  style={{ margin: 0 }}
                >
                  回写：{WRITE_BACK_STATUS_LABEL[taskDetail.write_back_status]}
                </Tag>
                <Tag
                  color={
                    taskDetail.status === TASK_STATUS.BLOCKED ? 'warning' : 'default'
                  }
                  style={{ margin: 0 }}
                >
                  {taskStatusLabel(taskDetail.status)}
                </Tag>
              </>
            )}

            {isBlocked && (
              <Button
                type="primary"
                danger
                icon={<SyncOutlined />}
                loading={retrying}
                onClick={handleRetryBlocked}
              >
                重跑任务
              </Button>
            )}

            <Button
              icon={<ReloadOutlined />}
              onClick={handleRefresh}
              loading={loading}
            >
              刷新
            </Button>
          </Space>
        </Space>

        {/* ---------------- 进度条 ---------------- */}
        {taskDetail && isInProgress && (
          <div style={{ marginTop: 10 }}>
            <Progress
              percent={taskDetail.progress}
              size="small"
              status="active"
              strokeColor={{ from: '#108ee9', to: '#87d068' }}
            />
            <Text type="secondary" style={{ fontSize: 11 }}>
              正在执行：{taskDetail.current_node ?? '排队中'}
              {' · '}
              页面每 3 秒自动刷新进度
            </Text>
          </div>
        )}
      </Card>

      {/* ================= 错误提示 ================= */}
      {error && (
        <Alert
          type="error"
          showIcon
          message="数据加载异常"
          description={error}
          action={
            <Button size="small" onClick={() => void initTask(taskNo)}>
              重新加载
            </Button>
          }
          closable
        />
      )}

      {/* ================= 双栏主体 ================= */}
      <Spin spinning={loading && !taskDetail} tip="正在加载合同与审查结果…">
        <div
          style={{
            display: 'flex',
            // 手机端改为纵向堆叠，避免 58%/42% 双栏在窄屏下被压成无法阅读的宽度
            flexDirection: isMobile ? 'column' : 'row',
            gap: 12,
            // 说明：桌面端固定双栏高度并各自内部滚动，
            // 使左右栏可以独立浏览而不互相牵动整页滚动条。
            // 手机端改为 auto，让两栏自然纵向排列、由页面整体滚动。
            height: isMobile ? 'auto' : `calc(100vh - ${WORKBENCH_PANEL_OFFSET_PX}px)`,
            minHeight: isMobile ? 'unset' : WORKBENCH_PANEL_OFFSET_PX,
          }}
        >
          {/* ---------------- 左栏：文档视窗 ---------------- */}
          <Card
            size="small"
            style={{
              flex: isMobile ? '1 1 auto' : '1 1 58%',
              minWidth: 0,
              overflow: 'hidden',
              // 手机端给文档视窗一个独立的可滚动高度，便于单手翻页
              height: isMobile ? '60vh' : undefined,
            }}
            styles={{ body: { padding: 0, height: '100%' } }}
          >
            <DocumentViewer
              pages={pages}
              locatedRisks={locatedRisks}
              selectedRiskId={selectedRiskId}
              targetPage={targetPage}
              highlightKeyword={highlightKeyword}
              // 说明：合同原件预览地址由 report API 的预签名链接体系提供；
              // 当前后端未开放合同原件直链，因此传 null 走文本模式。
              // 文本模式与风险项共享字符坐标系，高亮与定位精度不受影响。
              pdfUrl={null}
              contractTitle={taskDetail?.contract_title ?? ''}
              onSelectRisk={selectRiskByPosition}
              onPageChange={handlePageChange}
            />
          </Card>

          {/* ---------------- 右栏：审查结果与建议 ---------------- */}
          <Card
            size="small"
            style={{
              flex: isMobile ? '1 1 auto' : '1 1 42%',
              minWidth: 0,
              overflow: 'hidden',
              // 手机端给右栏固定高度并内部滚动，避免整页被超长卡片撑爆
              height: isMobile ? '55vh' : undefined,
            }}
            styles={{
              body: {
                padding: 0,
                height: isMobile ? '100%' : '100%',
              },
            }}
            title={
              <Space size={8}>
                <Text strong style={{ fontSize: 13 }}>
                  审查结果与修改建议
                </Text>
                <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
                  共 {locatedRisks.length} 项
                </Text>
              </Space>
            }
            extra={
              <Tooltip title="风险等级由规则引擎判定，按高→低排序">
                <Text type="secondary" style={{ fontSize: 11 }}>
                  按等级降序
                </Text>
              </Tooltip>
            }
          >
            {/* 等级筛选 */}
            <div
              style={{
                padding: '8px 12px',
                borderBottom: '1px solid #f0f0f0',
                overflowX: 'auto',
              }}
            >
              <Space size={8}>
                <Segmented
                  size="small"
                  value={levelFilter ?? 'all'}
                  onChange={(value) =>
                    setLevelFilter(value === 'all' ? null : (value as RiskLevel))
                  }
                  options={[
                    { label: `全部 ${locatedRisks.length}`, value: 'all' },
                    ...levelFilterOptions.map((option) => ({
                      label: option.label,
                      value: option.value,
                    })),
                  ]}
                />
              </Space>
            </div>

            {/* 风险卡片列表 */}
            <div
              ref={riskListRef}
              style={{ height: 'calc(100% - 45px)', overflow: 'auto', padding: 12 }}
            >
              {filteredRisks.length > 0 ? (
                filteredRisks.map((located) => (
                  <div
                    key={located.item.risk_id}
                    data-risk-card={located.item.risk_id}
                  >
                    <RiskCard
                      located={located}
                      selected={located.item.risk_id === selectedRiskId}
                      onSelect={() => selectRisk(located.item.risk_id)}
                      onAdopt={adoptRecommend}
                      onSave={saveSuggestion}
                    />
                  </div>
                ))
              ) : locatedRisks.length > 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      当前筛选条件下没有风险项
                    </Text>
                  }
                >
                  <Button size="small" onClick={() => setLevelFilter(null)}>
                    查看全部
                  </Button>
                </Empty>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    <div style={{ fontSize: 12 }}>
                      <div>
                        {isInProgress
                          ? '审查进行中，风险项将在规则判定完成后展示'
                          : isBlocked
                            ? '任务已阻塞，未产出审查结论'
                            : '本次审查未发现风险项'}
                      </div>
                      {!isInProgress && !isBlocked && (
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          规则引擎已完成全量校验
                        </Text>
                      )}
                    </div>
                  }
                />
              )}
            </div>
          </Card>
        </div>
      </Spin>

      {/* ================= 底栏：协同回写区 ================= */}
      <Card
        size="small"
        title={
          <Space size={8}>
            <Text strong style={{ fontSize: 13 }}>
              审查结论与协同回写
            </Text>
            <Tooltip title="智能审查仅输出意见与建议，不执行任何审批动作">
              <Tag color="blue" style={{ margin: 0, fontSize: 11 }}>
                只写评论 · 不做审批
              </Tag>
            </Tooltip>
          </Space>
        }
      >
        <ReviewFooterPanel
          taskDetail={taskDetail}
          overallLevel={overallLevel}
          distribution={riskDistribution}
          locatedRisks={locatedRisks}
          approvalHighlights={approvalHighlights}
          legalComment={legalComment}
          onCommentChange={setLegalComment}
          commentPreview={commentPreview}
          reportContent={reportContent}
          reportGenerated={Boolean(reportResult)}
          submitting={submitting}
          onPreview={previewComment}
          onWriteBack={writeBackComment}
          onGenerateReport={() => generateReport('md')}
        />
      </Card>
    </div>
  );
}
