// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/components/RiskCard.tsx
/**
 * 右栏：单个风险卡片
 *
 * 【文件职责】
 *   呈现一条风险的完整信息并承载相关操作：
 *     1. 风险描述（规则名称 + 说明）
 *     2. 风险等级（只读，由规则引擎定级）
 *     3. 法律依据
 *     4. 原文片段（证据文本）
 *     5. AI 推荐修改条款
 *     6. 一键复制采纳修改建议
 *     7. 人工编辑建议（仅建议类字段）
 *
 * 【权责红线】
 *   风险等级、判定结论、严重度一律只读展示，卡片内不提供任何修改入口。
 *   可编辑的只有 suggestion_text 与 legal_basis 两个建议类字段，
 *   这与后端 RiskItem 模型的字段级约束保持一致。
 *
 * 【"参考项"的视觉区分】
 *   reference_only 为 true 表示该条来自大模型/向量召回的候选，
 *   未经规则引擎确认，仅作参考。这类卡片整体降低视觉权重并明确标注，
 *   避免使用者把 AI 猜测当作正式判定结论。
 */

import { useState } from 'react';
import {
  App,
  Button,
  Card,
  Collapse,
  Descriptions,
  Input,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CopyOutlined,
  EditOutlined,
  EyeInvisibleOutlined,
  FileTextOutlined,
  SaveOutlined,
  WarningOutlined,
} from '@ant-design/icons';

import {
  CLAUSE_TYPE_LABEL,
  RISK_LEVEL_COLOR,
  RISK_LEVEL_DESCRIPTION,
  RISK_LEVEL_LABEL,
  RISK_LEVEL_TAG_COLOR,
} from '@/constants';
import { copyToClipboard, truncate } from '@/utils';
import type { LocatedRisk } from '@/types';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

interface RiskCardProps {
  /** 带定位信息的风险项 */
  located: LocatedRisk;
  /** 是否为当前选中卡片 */
  selected: boolean;
  /** 点击卡片回调（触发左栏定位） */
  onSelect: () => void;
  /** 采纳推荐条款回调 */
  onAdopt: (riskId: string, recommendedText: string) => Promise<void>;
  /** 保存建议回调（仅建议类字段） */
  onSave: (
    riskId: string,
    payload: { suggestion_text: string; legal_basis: string },
  ) => Promise<void>;
}

export default function RiskCard({
  located,
  selected,
  onSelect,
  onAdopt,
  onSave,
}: RiskCardProps): React.ReactElement {
  const { message } = App.useApp();
  const { item, page_no, paragraph_index, char_start, char_end } = located;

  /** 编辑模式 */
  const [editing, setEditing] = useState(false);
  /** 编辑中的建议文本 */
  const [suggestionDraft, setSuggestionDraft] = useState(item.suggestion_text);
  /** 编辑中的法律依据 */
  const [legalBasisDraft, setLegalBasisDraft] = useState(item.legal_basis);
  /** 保存中 */
  const [saving, setSaving] = useState(false);
  /** 采纳中 */
  const [adopting, setAdopting] = useState(false);

  const levelColor = RISK_LEVEL_COLOR[item.risk_level];
  const isReference = item.reference_only;
  /** 是否可定位到原文（有有效字符区间） */
  const locatable = char_start > 0 && char_end > char_start;

  /**
   * 组装可复制的完整建议文本
   *
   * 说明：复制内容面向法务的日常工作流——粘贴到 Word 修订稿或
   * 审批意见中。因此包含"风险点 + 依据 + 建议条款"三要素，
   * 而不是只复制一句建议，避免粘贴后需要回头再补上下文。
   */
  const buildCopyText = (recommendText: string): string => {
    const lines: string[] = [];
    lines.push(`【风险点】${item.rule_name}`);
    if (item.clause_type) {
      lines.push(
        `【条款类型】${CLAUSE_TYPE_LABEL[item.clause_type] ?? item.clause_type}`,
      );
    }
    lines.push(`【风险等级】${RISK_LEVEL_LABEL[item.risk_level]}（规则引擎定级）`);
    if (item.legal_basis) {
      lines.push(`【法律依据】${item.legal_basis}`);
    }
    if (item.evidence_text) {
      lines.push(`【原文片段】${truncate(item.evidence_text, 200)}`);
    }
    lines.push(`【修改建议】${recommendText}`);
    return lines.join('\n');
  };

  /** 复制推荐条款 */
  const handleCopy = async (): Promise<void> => {
    const succeeded = await copyToClipboard(buildCopyText(item.suggestion_text));
    if (succeeded) {
      message.success('修改建议已复制，可直接粘贴至修订稿');
    } else {
      message.error('复制失败，请手动选择文本复制');
    }
  };

  /** 一键采纳推荐条款 */
  const handleAdopt = (): void => {
    void (async () => {
      const recommended = item.suggestion_text?.trim();
      if (!recommended) {
        message.warning('该风险项暂无推荐条款，请先编辑建议内容');
        return;
      }

      setAdopting(true);
      try {
        await onAdopt(item.risk_id, recommended);
        message.success('已采纳该修改建议');
      } catch {
        message.error('采纳失败，请稍后重试');
      } finally {
        setAdopting(false);
      }
    })();
  };

  /** 保存人工编辑的建议 */
  const handleSave = (): void => {
    void (async () => {
      setSaving(true);
      try {
        await onSave(item.risk_id, {
          suggestion_text: suggestionDraft,
          legal_basis: legalBasisDraft,
        });
        setEditing(false);
        message.success('建议已保存');
      } catch {
        message.error('保存失败，请稍后重试');
      } finally {
        setSaving(false);
      }
    })();
  };

  /** 取消编辑，还原草稿 */
  const handleCancelEdit = (): void => {
    setSuggestionDraft(item.suggestion_text);
    setLegalBasisDraft(item.legal_basis);
    setEditing(false);
  };

  return (
    <Card
      size="small"
      onClick={onSelect}
      style={{
        marginBottom: 10,
        cursor: 'pointer',
        // 左侧色条直观标识风险等级，比仅靠 Tag 更快被扫读到
        borderLeft: `3px solid ${levelColor}`,
        // 参考项整体降低不透明度，视觉上后退
        opacity: isReference ? 0.82 : 1,
        // 选中态：外圈高亮 + 浅色底，与左栏高亮形成呼应
        boxShadow: selected ? `0 0 0 2px ${levelColor}44` : undefined,
        background: selected ? '#fafafa' : '#fff',
        transition: 'box-shadow 0.2s, background 0.2s',
      }}
      styles={{ body: { padding: 12 } }}
    >
      {/* ================= 卡片头部 ================= */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 8,
          marginBottom: 8,
        }}
      >
        <Space size={6} wrap>
          <Tag
            color={RISK_LEVEL_TAG_COLOR[item.risk_level]}
            style={{ margin: 0, fontWeight: 600 }}
          >
            {RISK_LEVEL_LABEL[item.risk_level]}
          </Tag>

          {/* 参考项明确标注来源，防止与正式判定混淆 */}
          {isReference && (
            <Tooltip title="该条由大模型/向量召回产生，未经规则引擎确认，仅供辅助参考，不参与风险定级">
              <Tag icon={<EyeInvisibleOutlined />} color="default" style={{ margin: 0 }}>
                参考项
              </Tag>
            </Tooltip>
          )}

          {item.rule_code && (
            <Tooltip title="规则编码，可用于在规则库中反查该条判定逻辑">
              <Text type="secondary" style={{ fontSize: 11 }}>
                {item.rule_code}
              </Text>
            </Tooltip>
          )}
        </Space>

        {/* 定位信息 */}
        <Tooltip
          title={
            locatable
              ? `原文定位：第 ${page_no} 页 · 段落 ${paragraph_index + 1} · 字符 ${char_start}-${char_end}`
              : '合同级风险，无单一原文位置'
          }
        >
          <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>
            <FileTextOutlined /> {locatable ? `P${page_no}` : '全文'}
          </Text>
        </Tooltip>
      </div>

      {/* ================= 风险描述 ================= */}
      <div style={{ marginBottom: 8 }}>
        <Text strong style={{ fontSize: 13 }}>
          {item.rule_name}
        </Text>
        {item.clause_type && (
          <Tag
            color="default"
            style={{ marginLeft: 6, fontSize: 11, lineHeight: '16px' }}
          >
            {CLAUSE_TYPE_LABEL[item.clause_type] ?? item.clause_type}
          </Tag>
        )}
        <Tooltip title={RISK_LEVEL_DESCRIPTION[item.risk_level]}>
          <WarningOutlined
            style={{ marginLeft: 6, color: levelColor, fontSize: 12 }}
          />
        </Tooltip>
      </div>

      {/* ================= 原文片段 ================= */}
      {item.evidence_text && (
        <div
          style={{
            background: '#fafafa',
            border: '1px solid #f0f0f0',
            borderRadius: 4,
            padding: '8px 10px',
            marginBottom: 8,
          }}
        >
          <Text type="secondary" style={{ fontSize: 11 }}>
            原文片段
          </Text>
          <Paragraph
            style={{
              margin: '4px 0 0',
              fontSize: 12,
              lineHeight: 1.8,
              color: '#595959',
              // 证据文本可能较长，限制高度避免单卡片占满整屏
              maxHeight: 120,
              overflow: 'auto',
            }}
          >
            {item.evidence_text}
          </Paragraph>
        </div>
      )}

      {/* ================= 法律依据 ================= */}
      {item.legal_basis && !editing && (
        <div style={{ marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            法律依据
          </Text>
          <div style={{ fontSize: 12, color: '#595959', lineHeight: 1.7 }}>
            {item.legal_basis}
          </div>
        </div>
      )}

      {/* ================= 推荐修改条款 ================= */}
      <div style={{ marginBottom: 8 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 4,
          }}
        >
          <Space size={4}>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {isReference ? 'AI 参考条款' : '推荐修改条款'}
            </Text>
            {!isReference && (
              <Tooltip title="该建议由大模型生成初稿，采纳后写入建议字段，不影响风险定级">
                <Tag
                  color="blue"
                  style={{ margin: 0, fontSize: 10, lineHeight: '14px' }}
                >
                  AI 初稿
                </Tag>
              </Tooltip>
            )}
          </Space>

          {!editing && (
            <Space size={4}>
              <Tooltip title="编辑建议内容（不改变风险定级）">
                <Button
                  type="text"
                  size="small"
                  icon={<EditOutlined />}
                  onClick={(event) => {
                    event.stopPropagation();
                    setEditing(true);
                  }}
                />
              </Tooltip>
              <Tooltip title="复制完整建议（含风险点、依据、条款）">
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={(event) => {
                    event.stopPropagation();
                    void handleCopy();
                  }}
                />
              </Tooltip>
              <Tooltip title="采纳该建议作为本风险项的处理意见">
                <Button
                  type="text"
                  size="small"
                  loading={adopting}
                  icon={<SaveOutlined />}
                  onClick={(event) => {
                    event.stopPropagation();
                    handleAdopt();
                  }}
                />
              </Tooltip>
            </Space>
          )}
        </div>

        {editing ? (
          <div onClick={(event) => event.stopPropagation()}>
            <TextArea
              value={suggestionDraft}
              onChange={(event) => setSuggestionDraft(event.target.value)}
              autoSize={{ minRows: 3, maxRows: 8 }}
              style={{ fontSize: 12 }}
              placeholder="请输入建议修改的条款内容"
            />
            <div style={{ marginTop: 6 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>
                法律依据
              </Text>
              <TextArea
                value={legalBasisDraft}
                onChange={(event) => setLegalBasisDraft(event.target.value)}
                autoSize={{ minRows: 1, maxRows: 3 }}
                style={{ fontSize: 12, marginTop: 4 }}
                placeholder="例如：《民法典》第 585 条"
              />
            </div>
            <Space size={8} style={{ marginTop: 8 }}>
              <Button
                type="primary"
                size="small"
                loading={saving}
                onClick={handleSave}
              >
                保存建议
              </Button>
              <Button size="small" onClick={handleCancelEdit} disabled={saving}>
                取消
              </Button>
              <Text type="secondary" style={{ fontSize: 11 }}>
                仅保存建议内容，风险等级由规则引擎判定
              </Text>
            </Space>
          </div>
        ) : (
          <div
            style={{
              background: `${levelColor}0d`,
              border: `1px solid ${levelColor}33`,
              borderRadius: 4,
              padding: '8px 10px',
              fontSize: 12,
              lineHeight: 1.8,
              color: '#262626',
              maxHeight: 200,
              overflow: 'auto',
            }}
          >
            {item.suggestion_text || (
              <Text type="secondary">暂无推荐条款，可点击编辑手动补充</Text>
            )}
          </div>
        )}
      </div>

      {/* ================= 命中来源明细 ================= */}
      {item.matched_sources.length > 0 && (
        <Collapse
          ghost
          size="small"
          items={[
            {
              key: 'sources',
              label: (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  命中来源明细（{item.matched_sources.length}）
                </Text>
              ),
              children: (
                <Descriptions
                  size="small"
                  column={1}
                  colon={false}
                  style={{ fontSize: 11 }}
                  items={[
                    {
                      key: 'sources',
                      label: '识别来源',
                      children: (
                        <Space size={4} wrap>
                          {item.matched_sources.map((source) => (
                            <Tag
                              key={source}
                              color={
                                source === 'rule_engine' ? 'red' : 'default'
                              }
                              style={{ fontSize: 10, margin: 0 }}
                            >
                              {source === 'rule_engine'
                                ? '规则引擎（终裁）'
                                : source === 'llm'
                                  ? '大模型（辅助）'
                                  : source === 'vector'
                                    ? '向量召回（辅助）'
                                    : source}
                            </Tag>
                          ))}
                        </Space>
                      ),
                    },
                    {
                      key: 'decision',
                      label: '判定来源',
                      children: (
                        <Text style={{ fontSize: 11 }}>{item.decision}</Text>
                      ),
                    },
                    ...(item.severity
                      ? [
                          {
                            key: 'severity',
                            label: '规则严重度',
                            children: (
                              <Text style={{ fontSize: 11 }}>
                                {item.severity}
                              </Text>
                            ),
                          },
                        ]
                      : []),
                  ]}
                />
              ),
            },
          ]}
        />
      )}
    </Card>
  );
}
