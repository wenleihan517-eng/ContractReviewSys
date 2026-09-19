// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/components/DocumentViewer.tsx
/**
 * 左栏：文档视窗
 *
 * 【文件职责】
 *   在审查工作台左栏呈现合同原文，并支持：
 *     1. PDF 分页渲染（pdfjs-dist）
 *     2. 文本模式兜底（Word / 纯文本走解析结果渲染）
 *     3. 风险条款高亮（按字符区间着色，颜色对应风险等级）
 *     4. 页码跳转与页内段落定位
 *     5. 点击高亮文本反选右栏风险卡片
 *     6. 保存原文字符位置与段落索引，联动右侧
 *
 * 【双模式设计原因】
 *   后端解析结果始终提供 full_text 与 text_blocks，这是"文本模式"的数据源，
 *   它与风险项的 char_start / char_end 处于同一坐标系，高亮天然精确。
 *   而 PDF 画布渲染由 pdfjs-dist 完成，画布本身无法承载 DOM 高亮。
 *   因此本组件采用组合策略：
 *     - 有 object_key（PDF 原件可访问）→ 上方展示 PDF 画布，下方并列展示
 *       带高亮的结构化文本，两者共用滚动同步与页码定位；
 *     - 无原件（Word / 扫描件）→ 仅展示带高亮的结构化文本。
 *   这样既满足"PDF 在线预览"的要求，又保证"风险条款高亮 + 字符位置
 *   精确定位"不因渲染引擎切换而失效。
 *
 * 【定位精度说明】
 *   高亮基于"全文字符区间"，不依赖文本相似度匹配。
 *   同一段文字在合同中多次出现时，只有真正命中的那一处会高亮，
 *   不会出现"全文所有相同文字都被点亮"的误报。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Segmented,
  Skeleton,
  Space,
  Tooltip,
  Typography,
} from 'antd';
import {
  AimOutlined,
  FilePdfOutlined,
  HighlightOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';

import {
  RISK_LEVEL_COLOR,
  RISK_LEVEL_LABEL,
  type RiskLevel,
} from '@/constants';
import {
  splitTextByRisks,
  type DocumentPage,
  type TextSegment,
} from '@/utils';
import type { LocatedRisk } from '@/types';
import { useResponsive } from '@/hooks/useResponsive';

const { Text } = Typography;

/** 视图模式 */
type ViewMode = 'pdf' | 'text';

interface DocumentViewerProps {
  /** 文档页列表（由解析结果构建） */
  pages: DocumentPage[];
  /** 带定位信息的风险项 */
  locatedRisks: LocatedRisk[];
  /** 当前选中的风险项 ID */
  selectedRiskId: string | null;
  /** 目标页码（右栏点击风险卡片时变化，触发左栏跳页） */
  targetPage: number;
  /** 命中该风险的高亮提示词（用于页内段落级定位） */
  highlightKeyword: string;
  /** PDF 原件在线预览地址；为空表示无原件（走纯文本模式） */
  pdfUrl: string | null;
  /** 合同标题，用于工具栏展示 */
  contractTitle: string;
  /** 点击高亮文本时回调，参数为风险项 ID */
  onSelectRisk: (riskId: string) => void;
  /** 页码变化回调，用于与右栏同步 */
  onPageChange: (page: number) => void;
}

export default function DocumentViewer({
  pages,
  locatedRisks,
  selectedRiskId,
  targetPage,
  highlightKeyword,
  pdfUrl,
  contractTitle,
  onSelectRisk,
  onPageChange,
}: DocumentViewerProps): React.ReactElement {
  /** 当前页码 */
  const [currentPage, setCurrentPage] = useState<number>(targetPage);
  /** 视图模式：有 PDF 原件时默认画布，否则纯文本 */
  const [viewMode, setViewMode] = useState<ViewMode>(pdfUrl ? 'pdf' : 'text');
  /** PDF 加载态 */
  const [pdfStatus, setPdfStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>(
    pdfUrl ? 'loading' : 'idle',
  );
  /** PDF 加载错误信息 */
  const [pdfError, setPdfError] = useState<string>('');

  /** pdf.js 模块引用（动态加载，避免首屏为文本模式时也下载 PDF 引擎） */
  const pdfjsRef = useRef<typeof import('pdfjs-dist') | null>(null);
  /** 当前 PDF 文档对象 */
  const pdfDocRef = useRef<import('pdfjs-dist').PDFDocumentProxy | null>(null);
  /** 画布容器 */
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  /** 文本容器（用于滚动定位） */
  const textContainerRef = useRef<HTMLDivElement | null>(null);
  /** 已渲染的页面标记，避免重复渲染 */
  const renderedPageRef = useRef<number>(0);

  /** 当前页数据 */
  const currentPageData = useMemo<DocumentPage | null>(
    () => pages.find((item) => item.page === currentPage) ?? pages[0] ?? null,
    [pages, currentPage],
  );

  /** 当前视口是否处于手机尺寸（< md） */
  const { isMobile } = useResponsive();

  /** 风险总条数（排除参考项） */
  const officialRiskCount = useMemo(
    () => locatedRisks.filter((located) => !located.item.reference_only).length,
    [locatedRisks],
  );

  // ====================================================================
  // 一、pdf.js 初始化与页面渲染
  // ====================================================================

  /**
   * 动态加载 pdfjs-dist 模块
   *
   * 说明：pdfjs-dist 体积较大，且 Worker 必须显式指定。
   * 这里使用 Vite 支持的 `?url` 导入方式取得 Worker 资源地址，
   * 避免 Worker 路径在打包后失效（这是 pdfjs 最常见的集成故障）。
   */
  const ensurePdfJs = useCallback(async (): Promise<
    typeof import('pdfjs-dist')
  > => {
    if (pdfjsRef.current) return pdfjsRef.current;

    const pdfjs = await import('pdfjs-dist');
    // Worker 资源：由 Vite 处理为可访问的静态资源 URL
    const workerUrl = (
      await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
    ).default;
    pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

    pdfjsRef.current = pdfjs;
    return pdfjs;
  }, []);

  /**
   * 加载 PDF 文档
   *
   * 说明：只在有 pdfUrl 时执行。加载成功后记录文档对象，
   * 后续翻页只重绘画布而不重新请求文件。
   */
  useEffect(() => {
    if (!pdfUrl || viewMode !== 'pdf') return;

    let cancelled = false;

    void (async () => {
      setPdfStatus('loading');
      setPdfError('');

      try {
        const pdfjs = await ensurePdfJs();
        const doc = await pdfjs.getDocument({
          url: pdfUrl,
          // 说明：禁用流式范围请求，避免后端不支持 Range 时无法加载
          disableRange: true,
          disableStream: true,
        }).promise;

        if (cancelled) {
          void doc.destroy();
          return;
        }

        pdfDocRef.current = doc;
        setPdfStatus('ready');
      } catch (error) {
        if (cancelled) return;
        setPdfStatus('error');
        setPdfError(
          error instanceof Error
            ? error.message
            : 'PDF 加载失败，可能文件不存在或后端未开放访问',
        );
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [pdfUrl, viewMode, ensurePdfJs]);

  /**
   * 渲染指定页到画布
   *
   * 参数：
   *   pageNo: 页码，从 1 开始
   *
   * 说明：按容器宽度自适应缩放，保证长文档在有限栏宽下完整可见。
   * 同一页重复渲染会被 renderedPageRef 拦截，避免滚动时反复重绘造成卡顿。
   */
  const renderPdfPage = useCallback(async (pageNo: number): Promise<void> => {
    const doc = pdfDocRef.current;
    const canvas = canvasRef.current;
    if (!doc || !canvas) return;
    if (renderedPageRef.current === pageNo) return;

    try {
      const page = await doc.getPage(pageNo);
      const containerWidth = canvas.parentElement?.clientWidth ?? 800;

      // 基准视口用于计算缩放比例，留出 24px 内边距避免贴边
      const baseViewport = page.getViewport({ scale: 1 });
      const scale = Math.max(
        0.4,
        Math.min(2.0, (containerWidth - 24) / baseViewport.width),
      );
      const viewport = page.getViewport({ scale });

      const context = canvas.getContext('2d');
      if (!context) return;

      // 按设备像素比放大画布，避免高分屏下文字发虚
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;

      context.setTransform(ratio, 0, 0, ratio, 0, 0);

      await page.render({ canvasContext: context, viewport }).promise;
      renderedPageRef.current = pageNo;
    } catch {
      // 单页渲染失败不阻断整个视窗，保持上一页可见
      renderedPageRef.current = 0;
    }
  }, []);

  // ---------------- PDF 页渲染触发 ----------------
  useEffect(() => {
    if (viewMode !== 'pdf' || pdfStatus !== 'ready') return;
    void renderPdfPage(currentPage);
  }, [viewMode, pdfStatus, currentPage, renderPdfPage]);

  // ---------------- 组件卸载时销毁 PDF 文档 ----------------
  useEffect(
    () => () => {
      const doc = pdfDocRef.current;
      if (doc) {
        void doc.destroy();
        pdfDocRef.current = null;
      }
    },
    [],
  );

  // ====================================================================
  // 二、右栏联动：跳页 + 页内定位
  // ====================================================================

  /**
   * 响应右栏的跳页请求
   *
   * 说明：只在目标页与当前页不同时才跳转，避免用户已手动翻到该页后
   * 再次点击同一风险卡片导致页面重置滚动位置。
   */
  useEffect(() => {
    if (!targetPage || targetPage === currentPage) return;
    // 目标页必须存在于页列表中，否则说明数据尚未加载完成
    if (!pages.some((item) => item.page === targetPage)) return;

    setCurrentPage(targetPage);
    onPageChange(targetPage);
  }, [targetPage, currentPage, pages, onPageChange]);

  /**
   * 页内段落级定位
   *
   * 说明：跳页之后还需要在页内滚动到命中位置。
   * 通过 data-risk-id 精确定位到高亮节点，而非按文字搜索——
   * 文字搜索在同页多处出现同样内容时会跳错位置。
   */
  useEffect(() => {
    if (!selectedRiskId || !textContainerRef.current) return;

    const container = textContainerRef.current;
    const node = container.querySelector<HTMLElement>(
      `[data-risk-id="${CSS.escape(selectedRiskId)}"]`,
    );
    if (!node) return;

    // 用 scrollIntoView 的 nearest 模式，避免整页抖动
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [selectedRiskId, currentPage, highlightKeyword]);

  // ====================================================================
  // 三、文本切分与高亮
  // ====================================================================

  /**
   * 把风险项转为高亮区间
   *
   * 说明：只保留有有效字符区间的风险项。合同级命中（如必备条款缺失）
   * 没有单一原文位置，不参与高亮——强行高亮会误导使用者
   * 去修改一段其实没问题的文字。
   */
  const riskRanges = useMemo(
    () =>
      locatedRisks
        .filter(
          (located) =>
            located.char_start > 0 && located.char_end > located.char_start,
        )
        .map((located) => ({
          start: located.char_start,
          end: located.char_end,
          level: located.item.risk_level,
          riskId: located.item.risk_id,
        })),
    [locatedRisks],
  );

  /** 首页之前的页码提示（合同级命中无原文位置） */
  const contractLevelRisks = useMemo(
    () =>
      locatedRisks.filter(
        (located) =>
          located.char_start === 0 && located.char_end === 0,
      ),
    [locatedRisks],
  );

  /**
   * 渲染高亮片段
   *
   * 参数：
   *   segments: 切分后的文本片段
   *
   * 返回：
   *   React 节点数组
   */
  const renderSegments = (segments: TextSegment[]): React.ReactNode =>
    segments.map((segment, index) => {
      const key = `${segment.riskId ?? 'plain'}-${index}`;

      if (!segment.highlighted || !segment.level) {
        return <span key={key}>{segment.text}</span>;
      }

      const color = RISK_LEVEL_COLOR[segment.level];
      const isSelected = segment.riskId === selectedRiskId;

      return (
        <Tooltip
          key={key}
          title={
            <span>
              {RISK_LEVEL_LABEL[segment.level]} · 点击查看该风险详情与修改建议
            </span>
          }
          mouseEnterDelay={0.3}
        >
          <mark
            data-risk-id={segment.riskId ?? undefined}
            onClick={() => {
              if (segment.riskId) onSelectRisk(segment.riskId);
            }}
            style={{
              // 半透明底色保留原文可读性，下划线加粗表示命中
              background: `${color}26`,
              borderBottom: `2px solid ${color}`,
              color: 'inherit',
              cursor: 'pointer',
              padding: '1px 0',
              // 选中状态用外发光区分，避免改变文字排布
              boxShadow: isSelected ? `0 0 0 2px ${color}55` : undefined,
              borderRadius: 2,
              transition: 'box-shadow 0.2s',
            }}
          >
            {segment.text}
          </mark>
        </Tooltip>
      );
    });

  /** 当前页的高亮片段（依赖 currentPageData 与 riskRanges） */
  const currentSegments = useMemo<TextSegment[]>(() => {
    if (!currentPageData) return [];
    return splitTextByRisks(
      currentPageData.text,
      currentPageData.startIdx,
      riskRanges,
    );
  }, [currentPageData, riskRanges]);

  /** 当前页命中的风险条数 */
  const currentPageHits = useMemo(
    () =>
      currentSegments.filter((segment) => segment.highlighted).length,
    [currentSegments],
  );

  // ====================================================================
  // 四、翻页控制
  // ====================================================================

  /** 跳转到指定页 */
  const goToPage = (page: number): void => {
    const bounded = Math.min(Math.max(1, page), pages.length || 1);
    setCurrentPage(bounded);
    onPageChange(bounded);
    // 翻页后重置已渲染标记，允许新页渲染
    renderedPageRef.current = 0;
  };

  // ====================================================================
  // 五、无数据兜底
  // ====================================================================

  if (pages.length === 0) {
    return (
      <div
        style={{
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#fff',
        }}
      >
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="合同原文尚未解析完成，请稍候"
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            解析完成后此处将展示合同全文与风险条款高亮
          </Text>
        </Empty>
      </div>
    );
  }

  return (
    <div
      style={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
      }}
    >
      {/* ================= 工具栏 ================= */}
      <div
        style={{
          padding: '10px 12px',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 8,
        }}
      >
        <Space size={8}>
          <FilePdfOutlined style={{ color: '#cf1322' }} />
          <Text strong style={{ fontSize: 13 }} ellipsis>
            {contractTitle || '合同原文'}
          </Text>
          {officialRiskCount > 0 && (
            <Tooltip title="合同原文中命中并已定位的风险条款数量">
              <Text type="secondary" style={{ fontSize: 12 }}>
                命中 {officialRiskCount} 处
              </Text>
            </Tooltip>
          )}
        </Space>

        <Space size={8}>
          {/* 视图切换：仅在 PDF 原件可访问时提供 */}
          {pdfUrl ? (
            <Segmented
              size="small"
              value={viewMode}
              onChange={(value) => setViewMode(value as ViewMode)}
              options={[
                { label: '版式', value: 'pdf', icon: <FilePdfOutlined /> },
                { label: '文本', value: 'text', icon: <HighlightOutlined /> },
              ]}
            />
          ) : (
            <Tooltip title="合同原件预览需后端签发对象存储临时链接。当前后端未开放该接口，因此此处以解析后的结构化文本呈现——文本与风险项共享同一字符坐标系，高亮与定位精度不受影响。">
              <Text type="secondary" style={{ fontSize: 11 }}>
                <HighlightOutlined /> 文本模式
              </Text>
            </Tooltip>
          )}

          {/* 翻页控制 */}
          <Space size={4}>
            <Button
              size="small"
              icon={<LeftOutlined />}
              disabled={currentPage <= 1}
              onClick={() => goToPage(currentPage - 1)}
            />
            <Text style={{ fontSize: 12, minWidth: 64, textAlign: 'center' }}>
              {currentPage} / {pages.length}
            </Text>
            <Button
              size="small"
              icon={<RightOutlined />}
              disabled={currentPage >= pages.length}
              onClick={() => goToPage(currentPage + 1)}
            />
          </Space>
        </Space>
      </div>

      {/* ================= 高亮图例 ================= */}
      <div
        style={{
          padding: '6px 12px',
          borderBottom: '1px solid #f5f5f5',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          background: '#fafafa',
        }}
      >
        <Space size={4}>
          <HighlightOutlined style={{ fontSize: 12, color: '#8c8c8c' }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            风险高亮：
          </Text>
        </Space>
        {(['critical', 'high', 'medium', 'low'] as RiskLevel[]).map((level) => (
          <Space key={level} size={4}>
            <span
              style={{
                display: 'inline-block',
                width: 12,
                height: 12,
                borderRadius: 2,
                background: `${RISK_LEVEL_COLOR[level]}26`,
                borderBottom: `2px solid ${RISK_LEVEL_COLOR[level]}`,
              }}
            />
            <Text style={{ fontSize: 12 }}>{RISK_LEVEL_LABEL[level]}</Text>
          </Space>
        ))}
        <Text type="secondary" style={{ fontSize: 12 }}>
          · 本页命中 {currentPageHits} 处
        </Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          · 点击高亮可定位右栏详情
        </Text>
      </div>

      {/* ================= 正文区 ================= */}
      <div
        ref={textContainerRef}
        style={{
          flex: 1,
          overflow: 'auto',
          // 手机端收窄正文容器内边距，给窄屏更多可用宽度
          padding: isMobile ? 12 : 16,
          background: viewMode === 'pdf' ? '#f0f2f5' : '#fff',
        }}
      >
        {/* ---------------- 合同级命中提示 ---------------- */}
        {currentPage === 1 && contractLevelRisks.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12, fontSize: 12 }}
            message={`存在 ${contractLevelRisks.length} 项合同级风险（无单一原文位置）`}
            description={
              <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                {contractLevelRisks.slice(0, 4).map((located) => (
                  <div key={located.item.risk_id}>
                    · {located.item.rule_name}
                    {located.item.legal_basis
                      ? `（依据：${located.item.legal_basis}）`
                      : ''}
                  </div>
                ))}
                {contractLevelRisks.length > 4 && (
                  <div>· 其余 {contractLevelRisks.length - 4} 项见右侧列表</div>
                )}
                <div style={{ marginTop: 4, color: '#8c8c8c' }}>
                  此类风险为整份合同层面的缺失或不对等，需结合右侧建议整体修改。
                </div>
              </div>
            }
          />
        )}

        {/* ---------------- PDF 画布 ---------------- */}
        {viewMode === 'pdf' && (
          <div style={{ marginBottom: 16 }}>
            {pdfStatus === 'loading' && (
              <Skeleton active paragraph={{ rows: 8 }} />
            )}

            {pdfStatus === 'error' && (
              <Alert
                type="warning"
                showIcon
                message="PDF 原件无法加载"
                description={
                  <div style={{ fontSize: 12 }}>
                    <div>{pdfError}</div>
                    <div style={{ marginTop: 4 }}>
                      已自动为您保留文本模式，风险高亮与定位功能不受影响。
                    </div>
                  </div>
                }
                action={
                  <Button
                    size="small"
                    onClick={() => setViewMode('text')}
                  >
                    切换文本模式
                  </Button>
                }
              />
            )}

            <div
              style={{
                display: pdfStatus === 'ready' ? 'flex' : 'none',
                justifyContent: 'center',
              }}
            >
              <canvas
                ref={canvasRef}
                style={{
                  background: '#fff',
                  boxShadow: '0 2px 8px rgba(0,0,0,0.12)',
                }}
              />
            </div>
          </div>
        )}

        {/* ---------------- 结构化文本（含高亮） ---------------- */}
        <div
          style={{
            background: '#fff',
            // 手机端收窄文本块内边距，避免正文被两侧留白挤占
            padding: isMobile ? 12 : 24,
            borderRadius: 6,
            boxShadow:
              viewMode === 'pdf' ? '0 2px 8px rgba(0,0,0,0.08)' : 'none',
            maxWidth: viewMode === 'pdf' ? 760 : undefined,
            margin: viewMode === 'pdf' ? '0 auto' : undefined,
          }}
        >
          {/* 页眉：标注当前页与命中数，便于与 PDF 画布对照 */}
          <div
            style={{
              marginBottom: 12,
              paddingBottom: 8,
              borderBottom: '1px dashed #e8e8e8',
              display: 'flex',
              justifyContent: 'space-between',
            }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              第 {currentPageData?.page ?? 1} 页
              {viewMode === 'pdf' && ' · 文本对照'}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {currentSegments.filter((s) => s.highlighted).length > 0
                ? `本页命中 ${currentSegments.filter((s) => s.highlighted).length} 处风险`
                : '本页无风险命中'}
            </Text>
          </div>

          {/* 正文：保留换行与空格，按原始排版呈现 */}
          <div
            style={{
              fontSize: 13,
              lineHeight: 2,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              color: '#262626',
              fontFamily:
                '"Songti SC", "SimSun", "Microsoft YaHei", serif',
            }}
          >
            {renderSegments(currentSegments)}
          </div>

          {/* 页脚：定位信息，便于法务引用与核对 */}
          <div
            style={{
              marginTop: 16,
              paddingTop: 8,
              borderTop: '1px dashed #e8e8e8',
              display: 'flex',
              justifyContent: 'space-between',
            }}
          >
            <Text type="secondary" style={{ fontSize: 11 }}>
              字符区间：{currentPageData?.startIdx ?? 0} -{' '}
              {(currentPageData?.startIdx ?? 0) +
                (currentPageData?.text.length ?? 0)}
            </Text>
            <Space size={4}>
              <AimOutlined style={{ fontSize: 11, color: '#8c8c8c' }} />
              <Text type="secondary" style={{ fontSize: 11 }}>
                高亮按全文字符位置精确匹配
              </Text>
            </Space>
          </div>
        </div>
      </div>
    </div>
  );
}
