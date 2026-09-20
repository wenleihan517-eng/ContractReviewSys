// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/utils/index.ts
/**
 * 通用工具函数
 *
 * 【文件职责】
 *   集中维护格式化、排序、区间运算、文本切分等纯函数，供各页面复用。
 *   纯函数不依赖 React 与浏览器 API，便于单独推断与测试。
 */

import dayjs from 'dayjs';

import {
  CLAUSE_TYPE_LABEL,
  CONTRACT_TYPE_LABEL,
  RISK_LEVEL,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_ORDER,
  TASK_STATUS_LABEL,
  type ContractType,
  type RiskLevel,
  type TaskStatus,
} from '@/constants';
import type { RiskItemResponse, TextBlockItem } from '@/types';

// ======================================================================
// 一、格式化
// ======================================================================

/**
 * 格式化金额为人民币展示文本
 *
 * 说明：境内财务场景按"元 → 万元/亿元"分级展示，避免长数字难以阅读；
 * 金额为 0 或非法值时返回占位符，不展示为 "¥0.00" 造成误读。
 *
 * 参数：
 *   amount: 金额（元）
 *   currency: 币种，默认 CNY
 *
 * 返回：
 *   带千分位与单位的展示文本
 */
export function formatAmount(amount: number, currency = 'CNY'): string {
  if (!Number.isFinite(amount) || amount <= 0) return '—';

  const symbol = currency === 'CNY' ? '¥' : `${currency} `;

  if (amount >= 100000000) {
    return `${symbol}${(amount / 100000000).toFixed(2)} 亿元`;
  }
  if (amount >= 10000) {
    return `${symbol}${(amount / 10000).toFixed(2)} 万元`;
  }
  return `${symbol}${amount.toFixed(2)}`;
}

/**
 * 格式化金额为完整元值（用于详情抽屉，避免单位换算歧义）
 *
 * 参数：
 *   amount: 金额（元）
 *
 * 返回：
 *   带千分位的元值文本
 */
export function formatAmountExact(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return '—';
  return `¥${amount.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/**
 * 格式化时间
 *
 * 参数：
 *   value: ISO 时间串或 null
 *   template: dayjs 模板，默认精确到秒
 *
 * 返回：
 *   格式化文本，空值返回占位符
 */
export function formatTime(
  value: string | null | undefined,
  template = 'YYYY-MM-DD HH:mm:ss',
): string {
  if (!value) return '—';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format(template) : '—';
}

/**
 * 格式化文件大小为可读文本
 *
 * 参数：
 *   bytes: 字节数
 *
 * 返回：
 *   B / KB / MB 文本
 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * 截断文本
 *
 * 参数：
 *   text: 原文本
 *   maxLength: 最大长度
 *
 * 返回：
 *   超长时追加省略号
 */
export function truncate(text: string, maxLength = 80): string {
  if (!text) return '';
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

// ======================================================================
// 二、枚举展示
// ======================================================================

/**
 * 获取风险等级中文标签
 *
 * 参数：
 *   level: 风险等级或 null
 *
 * 返回：
 *   中文标签，未知值原样返回，空值返回"未判定"
 */
export function riskLevelLabel(level: RiskLevel | string | null): string {
  if (!level) return '未判定';
  return RISK_LEVEL_LABEL[level as RiskLevel] ?? level;
}

/**
 * 获取任务状态中文标签
 *
 * 参数：
 *   status: 任务状态
 *
 * 返回：
 *   中文标签，未知值原样返回
 */
export function taskStatusLabel(status: TaskStatus | string): string {
  return TASK_STATUS_LABEL[status as TaskStatus] ?? status;
}

/**
 * 获取合同类型中文标签
 *
 * 参数：
 *   type: 合同类型
 *
 * 返回：
 *   中文标签，未知值原样返回
 */
export function contractTypeLabel(type: ContractType | string | null): string {
  if (!type) return '—';
  return CONTRACT_TYPE_LABEL[type] ?? type;
}

/**
 * 获取条款类型中文标签
 *
 * 参数：
 *   type: 条款类型
 *
 * 返回：
 *   中文标签，未知值"其他/未分类"
 */
export function clauseTypeLabel(type: string | null): string {
  if (!type) return '未分类';
  return CLAUSE_TYPE_LABEL[type] ?? type;
}

// ======================================================================
// 三、风险等级运算
// ======================================================================

/**
 * 计算风险项集合中的最高风险等级
 *
 * 说明：与后端"从严定级"口径一致——取所有命中项中的最高等级。
 * 参考项（reference_only）不参与计算，避免 AI 候选抬高综合等级。
 *
 * 参数：
 *   items: 风险项列表
 *
 * 返回：
 *   最高风险等级；无有效项时返回 null
 */
export function computeHighestRiskLevel(
  items: RiskItemResponse[],
): RiskLevel | null {
  const official = items.filter((item) => !item.reference_only);
  if (official.length === 0) return null;

  return official.reduce<RiskLevel>((highest, item) => {
    const current = item.risk_level;
    return RISK_LEVEL_ORDER[current] > RISK_LEVEL_ORDER[highest]
      ? current
      : highest;
  }, RISK_LEVEL.LOW);
}

/**
 * 统计风险等级分布
 *
 * 参数：
 *   items: 风险项列表
 *
 * 返回：
 *   各等级数量的映射
 */
export function countRiskLevels(
  items: RiskItemResponse[],
): Record<RiskLevel, number> {
  const result: Record<RiskLevel, number> = {
    low: 0,
    medium: 0,
    high: 0,
    critical: 0,
  };

  items.forEach((item) => {
    if (item.reference_only) return;
    if (result[item.risk_level] !== undefined) {
      result[item.risk_level] += 1;
    }
  });

  return result;
}

/**
 * 按风险等级降序排序风险项
 *
 * 说明：同级按页码与字符位置升序，保证"同一处的风险靠在一起"且顺序稳定，
 * 便于与左侧文档视窗的阅读顺序对应。
 *
 * 参数：
 *   items: 风险项列表
 *
 * 返回：
 *   排序后的新数组（不修改入参）
 */
export function sortRiskItems(items: RiskItemResponse[]): RiskItemResponse[] {
  return [...items].sort((a, b) => {
    // 1. 规则命中项优先于参考项
    if (a.reference_only !== b.reference_only) {
      return a.reference_only ? 1 : -1;
    }
    // 2. 风险等级降序
    const levelDiff =
      RISK_LEVEL_ORDER[b.risk_level] - RISK_LEVEL_ORDER[a.risk_level];
    if (levelDiff !== 0) return levelDiff;
    // 3. 页码升序
    const pageDiff = (a.page_no ?? 0) - (b.page_no ?? 0);
    if (pageDiff !== 0) return pageDiff;
    // 4. 字符位置升序
    return (a.char_start ?? 0) - (b.char_start ?? 0);
  });
}

// ======================================================================
// 四、字符区间与文本定位
// ======================================================================

/**
 * 判断两个字符区间是否重叠
 *
 * 参数：
 *   aStart / aEnd: 区间 A（左闭右开）
 *   bStart / bEnd: 区间 B（左闭右开）
 *
 * 返回：
 *   是否重叠
 */
export function isRangeOverlap(
  aStart: number,
  aEnd: number,
  bStart: number,
  bEnd: number,
): boolean {
  return aStart < bEnd && bStart < aEnd;
}

/**
 * 从文本块列表中反查某个字符位置所属的文本块
 *
 * 说明：风险项给出全文字符区间与页码，段落索引需由文本块反查得到，
 * 这是左右栏联动的关键——右栏点击风险卡片时，左栏据此定位到对应段落。
 *
 * 参数：
 *   blocks: 文本块列表
 *   charStart: 目标字符起始位置
 *
 * 返回：
 *   命中的文本块；无匹配返回 null
 */
export function findBlockByCharPosition(
  blocks: TextBlockItem[],
  charStart: number,
): TextBlockItem | null {
  for (const block of blocks) {
    if (charStart >= block.start_idx && charStart < block.end_idx) {
      return block;
    }
  }
  return null;
}

/**
 * 解析风险项的定位信息
 *
 * 说明：优先使用风险项自带的页码与字符区间；页码缺失时通过文本块反查补齐。
 * 字符区间缺失时退化为按证据文本在全文中的查找结果定位。
 *
 * 参数：
 *   item: 风险项
 *   blocks: 文本块列表
 *   fullText: 合同全文
 *
 * 返回：
 *   { pageNo, paragraphIndex, charStart, charEnd }，无法定位时各字段为兜底值
 */
export function resolveRiskLocation(
  item: RiskItemResponse,
  blocks: TextBlockItem[],
  fullText: string,
): {
  pageNo: number;
  paragraphIndex: number;
  charStart: number;
  charEnd: number;
} {
  // ---------------- 情形一：区间完整，直接使用 ----------------
  if (
    item.char_start !== null &&
    item.char_end !== null &&
    item.char_end > item.char_start
  ) {
    const block = findBlockByCharPosition(blocks, item.char_start);
    return {
      pageNo: item.page_no ?? block?.page ?? 1,
      paragraphIndex: block?.paragraph_index ?? 0,
      charStart: item.char_start,
      charEnd: item.char_end,
    };
  }

  // ---------------- 情形二：缺失区间，按证据文本反查 ----------------
  // 说明：合同级命中的规则（如必备条款缺失）天然没有字符区间，
  // 此时定位到全文起点并在 UI 上标注"合同级命中，无单一原文位置"。
  const evidenceRaw = (item.evidence_text || '').trim();
  if (evidenceRaw && fullText) {
    const foundIndex = fullText.indexOf(evidenceRaw);
    if (foundIndex >= 0) {
      const block = findBlockByCharPosition(blocks, foundIndex);
      return {
        pageNo: item.page_no ?? block?.page ?? 1,
        paragraphIndex: block?.paragraph_index ?? 0,
        charStart: foundIndex,
        charEnd: foundIndex + evidenceRaw.length,
      };
    }
  }

  // ---------------- 情形三：完全无法定位 ----------------
  return {
    pageNo: item.page_no ?? 1,
    paragraphIndex: 0,
    charStart: 0,
    charEnd: 0,
  };
}

/**
 * 文本片段类型（用于高亮渲染）
 */
export interface TextSegment {
  /** 片段文本 */
  text: string;
  /** 是否命中风险（命中时高亮） */
  highlighted: boolean;
  /** 命中该片段的风险等级，未命中为 null */
  level: RiskLevel | null;
  /** 命中该片段的风险项 ID，未命中为 null */
  riskId: string | null;
}

/**
 * 把原始文本按风险区间切分为可渲染片段
 *
 * 【核心算法】
 *   1. 把与当前文本区间有交集的风险区间裁剪到本区间内
 *   2. 按起始位置升序排序，区间重叠时取风险等级更高的那个
 *   3. 逐个片段推进游标，未命中部分产出 highlighted=false 的普通片段
 *
 * 说明：这是"风险条款高亮"的核心实现。必须处理区间重叠与越界，
 * 否则会出现高亮错位或文本丢失。
 *
 * 参数：
 *   text: 原始文本
 *   textOffset: text 在合同全文中的起始偏移（用于把局部区间换算为全文区间）
 *   risks: 风险区间列表，元素含 start / end / level / riskId
 *
 * 返回：
 *   片段列表，按顺序拼接后与原始文本完全一致
 */
export function splitTextByRisks(
  text: string,
  textOffset: number,
  risks: Array<{
    start: number;
    end: number;
    level: RiskLevel;
    riskId: string;
  }>,
): TextSegment[] {
  if (!text) return [];

  const textEnd = textOffset + text.length;

  // ---------------- 第一步：裁剪并过滤有效区间 ----------------
  const ranges = risks
    .filter((risk) => risk.end > textOffset && risk.start < textEnd)
    .map((risk) => ({
      // 裁剪到本文本区间内，并换算为相对偏移
      start: Math.max(risk.start, textOffset) - textOffset,
      end: Math.min(risk.end, textEnd) - textOffset,
      level: risk.level,
      riskId: risk.riskId,
    }))
    .filter((range) => range.end > range.start);

  if (ranges.length === 0) {
    return [{ text, highlighted: false, level: null, riskId: null }];
  }

  // ---------------- 第二步：排序（同级时保留先出现的） ----------------
  ranges.sort((a, b) => {
    if (a.start !== b.start) return a.start - b.start;
    return RISK_LEVEL_ORDER[b.level] - RISK_LEVEL_ORDER[a.level];
  });

  // ---------------- 第三步：切分 ----------------
  const segments: TextSegment[] = [];
  let cursor = 0;

  for (const range of ranges) {
    // 跳过已被前一个区间覆盖的区间（重叠处理：先到者优先，且已按等级排序）
    if (range.start < cursor) continue;

    // 未命中部分
    if (range.start > cursor) {
      segments.push({
        text: text.slice(cursor, range.start),
        highlighted: false,
        level: null,
        riskId: null,
      });
    }

    // 命中部分
    segments.push({
      text: text.slice(range.start, range.end),
      highlighted: true,
      level: range.level,
      riskId: range.riskId,
    });

    cursor = range.end;
  }

  // 尾段
  if (cursor < text.length) {
    segments.push({
      text: text.slice(cursor),
      highlighted: false,
      level: null,
      riskId: null,
    });
  }

  return segments;
}

// ======================================================================
// 五、合同文本分页构造
// ======================================================================

/** 分页文本页 */
export interface DocumentPage {
  /** 页码，从 1 开始 */
  page: number;
  /** 该页文本 */
  text: string;
  /** 该页文本在合同全文中的起始偏移 */
  startIdx: number;
}

/**
 * 把合同全文按页码切分为页列表
 *
 * 【优先策略】
 *   若解析结果提供了 text_blocks（含 page 与 start_idx），
 *   则按块所属页码聚合，保证页边界与真实 PDF 分页一致。
 *
 * 【兜底策略】
 *   无文本块时按固定字符数逻辑分页，保证 Word 等无固定分页的文档也能翻页浏览。
 *
 * 参数：
 *   fullText: 合同全文
 *   blocks: 文本块列表（可为空）
 *   pageCount: 后端给出的页数
 *   fallbackCharsPerPage: 兜底逻辑分页的每页字符数
 *
 * 返回：
 *   页列表，按页码升序
 */
export function buildDocumentPages(
  fullText: string,
  blocks: TextBlockItem[],
  pageCount: number,
  fallbackCharsPerPage = 2000,
): DocumentPage[] {
  // ---------------- 优先：按文本块的真实页码聚合 ----------------
  if (blocks.length > 0) {
    const pageMap = new Map<number, TextBlockItem[]>();
    blocks.forEach((block) => {
      const list = pageMap.get(block.page);
      if (list) {
        list.push(block);
      } else {
        pageMap.set(block.page, [block]);
      }
    });

    return Array.from(pageMap.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([page, pageBlocks]) => {
        const sorted = [...pageBlocks].sort((a, b) => a.start_idx - b.start_idx);
        const startIdx = sorted[0].start_idx;
        const endIdx = sorted[sorted.length - 1].end_idx;
        return {
          page,
          text: fullText.slice(startIdx, endIdx),
          startIdx,
        };
      });
  }

  // ---------------- 兜底：固定字符数逻辑分页 ----------------
  const effectivePageCount =
    pageCount > 0
      ? pageCount
      : Math.max(1, Math.ceil(fullText.length / fallbackCharsPerPage));

  const pages: DocumentPage[] = [];
  const step = Math.max(
    1,
    Math.ceil(fullText.length / effectivePageCount) || fallbackCharsPerPage,
  );

  for (let index = 0; index < effectivePageCount; index += 1) {
    const startIdx = index * step;
    if (startIdx >= fullText.length) break;
    pages.push({
      page: index + 1,
      text: fullText.slice(startIdx, startIdx + step),
      startIdx,
    });
  }

  return pages.length > 0
    ? pages
    : [{ page: 1, text: fullText, startIdx: 0 }];
}

/**
 * 根据字符位置反查所在页码
 *
 * 参数：
 *   pages: 页列表
 *   charPosition: 全文字符位置
 *
 * 返回：
 *   页码，从 1 开始；未命中返回 1
 */
export function findPageByCharPosition(
  pages: DocumentPage[],
  charPosition: number,
): number {
  for (let index = pages.length - 1; index >= 0; index -= 1) {
    if (charPosition >= pages[index].startIdx) {
      return pages[index].page;
    }
  }
  return 1;
}

// ======================================================================
// 六、剪贴板
// ======================================================================

/**
 * 复制文本到剪贴板
 *
 * 说明：优先使用 Clipboard API；在非 HTTPS 环境下 Clipboard API 不可用，
 * 降级为 textarea + execCommand 方案，保证内网部署也能复制成功。
 *
 * 参数：
 *   text: 待复制文本
 *
 * 返回：
 *   是否复制成功
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;

  // ---------------- 优先：现代 Clipboard API ----------------
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 继续尝试降级方案
    }
  }

  // ---------------- 降级：临时 textarea ----------------
  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    // 移出可视区域避免页面跳动
    textarea.style.position = 'fixed';
    textarea.style.top = '-9999px';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    const succeeded = document.execCommand('copy');
    document.body.removeChild(textarea);
    return succeeded;
  } catch {
    return false;
  }
}

/**
 * 触发浏览器下载
 *
 * 参数：
 *   content: 文件内容
 *   fileName: 文件名
 *   mimeType: MIME 类型
 *
 * 返回：
 *   无返回值
 */
export function downloadFile(
  content: string,
  fileName: string,
  mimeType = 'text/markdown;charset=utf-8',
): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // 释放对象 URL，避免内存泄漏
  URL.revokeObjectURL(url);
}
