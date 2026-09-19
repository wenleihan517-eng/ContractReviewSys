// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/components/UploadContractModal.tsx
/**
 * 手动上传合同弹窗（上传免填版）
 *
 * 【文件职责】
 *   提供"手动上传合同 → 创建审查任务"的完整链路：
 *     1. 选择合同文件（唯一必做动作）
 *     2. 上传合同文件到对象存储，取得 object_key
 *     3. 创建审查任务
 *     4. 触发文档解析（后端解析完成后自动识别元数据并自动启动审查）
 *
 * 【为什么不再要求填写合同名称 / 合同类型（2026-09-19 用户需求）】
 *   解析完成后，后端 metadata_extractor 会从合同原文自动识别标题、
 *   金额、相对方等元数据（含证据留痕）并回填任务主档；合同类型由
 *   关键词轻量识别标注。因此本弹窗只保留"选文件"一个动作，
 *   原先的 9 个表单字段全部移除。
 *
 * 【为什么先上传再建任务】
 *   任务创建接口需要 file_object_key。先上传拿到对象键，
 *   再创建任务并携带该键，可保证任务创建成功时文件必定已就位，
 *   不会出现"任务存在但文件缺失"的中间态。
 */

import { useState } from 'react';
import {
  Alert,
  App,
  Button,
  Modal,
  Progress,
  Space,
  Steps,
  Upload,
} from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';

import { parseApi, taskApi } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import {
  ALLOWED_UPLOAD_EXTS,
  MAX_UPLOAD_SIZE,
  SCANNED_IMAGE_EXTS,
} from '@/constants';
import { formatFileSize } from '@/utils';
// 紧凑拖拽区样式：antd Dragger 默认 height:100% 高度链会被弹窗父容器意外拉伸，
// 此处以 height:auto 打断并压缩图标与内边距，仅作用于 rootClassName 标记的实例。
import '@/styles/upload-compact.css';

const { Dragger } = Upload;

interface UploadContractModalProps {
  /** 弹窗可见性 */
  open: boolean;
  /** 关闭回调 */
  onClose: () => void;
  /** 创建成功回调，参数为新任务编号 */
  onSuccess: (taskNo: string) => void;
}

/** 流程步骤定义 */
const STEP_ITEMS = [
  { title: '上传文件', description: '存入对象存储' },
  { title: '创建任务', description: '写入审查任务' },
  { title: '触发解析', description: '自动识别并启动审查' },
];

export default function UploadContractModal({
  open,
  onClose,
  onSuccess,
}: UploadContractModalProps): React.ReactElement {
  const { message } = App.useApp();

  /** 已选文件列表（Ant Design Upload 受控模式，始终只保留 1 个文件） */
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  /** 当前步骤（0 起始） */
  const [currentStep, setCurrentStep] = useState(0);
  /** 上传进度百分比 */
  const [uploadPercent, setUploadPercent] = useState(0);
  /** 提交中 */
  const [submitting, setSubmitting] = useState(false);
  /** 失败信息（含失败所在步骤） */
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  /** 重置弹窗内部状态 */
  const resetInternalState = (): void => {
    setFileList([]);
    setCurrentStep(0);
    setUploadPercent(0);
    setSubmitting(false);
    setErrorMessage(null);
  };

  /** 关闭并清理 */
  const handleClose = (): void => {
    if (submitting) return; // 提交中禁止关闭，避免中断流程留下半成品
    resetInternalState();
    onClose();
  };

  /**
   * 校验文件合法性
   *
   * 说明：前置校验可以避免把不合规文件传到对象存储后再失败，
   * 既省带宽也省存储。
   *
   * 参数：
   *   file: 待校验文件
   *
   * 返回：
   *   通过返回 true；否则调用 onError 并返回 false
   */
  const validateFile = (
    file: File,
    onError: (msg: string) => void,
  ): boolean => {
    const ext = `.${file.name.split('.').pop()?.toLowerCase() ?? ''}`;
    if (!ALLOWED_UPLOAD_EXTS.includes(ext)) {
      onError(
        `不支持的文件格式 ${ext}，仅支持 ${ALLOWED_UPLOAD_EXTS.join(' / ')}`,
      );
      return false;
    }
    if (file.size > MAX_UPLOAD_SIZE) {
      onError(
        `文件大小 ${formatFileSize(file.size)} 超出上限 ${formatFileSize(MAX_UPLOAD_SIZE)}`,
      );
      return false;
    }
    if (file.size === 0) {
      onError('文件内容为空，请检查文件是否损坏');
      return false;
    }

    // 图片类文件没有文字层，必须经 OCR 才能抽取文本。
    // 这里提前提示（而非静默失败），避免使用者提交了扫描件却在触发解析时
    // 才收到"OCR 引擎不可用"，来回排查一轮。
    if (SCANNED_IMAGE_EXTS.includes(ext)) {
      message.info(
        '图片类文件将走 OCR 识别，需后端已部署 OCR 引擎；若未部署会解析失败，建议优先上传 PDF 原件。',
      );
    }
    return true;
  };

  /**
   * 提交完整送审流程
   *
   * 流程：
   *   步骤一 —— 上传文件，取得 object_key 与 source_type
   *   步骤二 —— 创建审查任务，取得 task_id / task_no
   *   步骤三 —— 触发文档解析，任务正式进入流水线
   */
  const handleSubmit = async (): Promise<void> => {
    const file = fileList[0]?.originFileObj;
    if (!file) {
      // 说明：除了「没选文件」，还有一种情况需要区分——文件已在列表中，
      // 但 originFileObj 缺失（例如浏览器未提供 filesystem 引用）。
      // 此时直接提示「请先选择文件」会让使用者困惑（明明选了），
      // 因此给出更准确的指引。
      if (fileList.length > 0) {
        message.warning('文件引用已失效，请重新选择一次合同文件');
      } else {
        message.warning('请先选择要送审的合同文件');
      }
      return;
    }

    setSubmitting(true);
    setErrorMessage(null);

    try {
      // ================= 步骤一：上传文件 =================
      setCurrentStep(0);
      const uploaded = await parseApi.upload(file, (percent) => {
        setUploadPercent(percent);
      });

      // ================= 步骤二：创建任务 =================
      // 说明：合同名称/类型不再由用户填写（上传免填改造），
      //   标题暂以文件名占位，解析完成后由后端自动识别覆盖；
      //   业务字段（金额/相对方等）同样由系统从原文自动识别。
      setCurrentStep(1);
      const created = await taskApi.create({
        contract_id: uploaded.object_key,
        contract_title: uploaded.file_name.replace(/\.[^.]+$/, ''),
        currency: 'CNY',
        source_type: uploaded.source_type,
        file_object_key: uploaded.object_key,
      });

      // ================= 步骤三：触发解析 =================
      // 说明：解析完成后后端会自动识别合同元数据并自动启动审查，
      //   无需（也没有）单独的"触发审查"前端步骤。
      setCurrentStep(2);
      const triggered = await parseApi.trigger(created.task_id, {
        task_id: created.task_id,
        object_key: uploaded.object_key,
        source_type: uploaded.source_type,
        file_name: uploaded.file_name,
      });

      // 【关键】受理失败必须视为失败，不能当成成功。
      //   背景：解析接口在「未受理」时仍返回业务码 0（code=0），
      //   只是把 accepted 置为 false 并在 message 里说明原因
      //   （典型场景：上传了图片扫描件但 OCR 引擎不可用、文件加密、
      //     内容为空等）。统一响应拦截器只校验 code，因此不会抛错，
      //   若这里不显式判断，界面会弹出"已提交审查"的成功提示，
      //   而任务实际停留在解析失败状态 —— 表现为"点了没反应"。
      if (triggered && triggered.accepted === false) {
        setErrorMessage(
          `「触发解析」阶段失败：${triggered.message || '后端未受理该解析请求'}`,
        );
        setSubmitting(false);
        return;
      }

      message.success('合同已提交审查，系统将自动识别并启动审查流程');
      resetInternalState();
      onSuccess(created.task_no);
      onClose();
    } catch (error) {
      // 明确告知失败发生在哪一步，便于使用者判断是否需要重新上传文件
      const stepName = STEP_ITEMS[currentStep]?.title ?? '处理';
      const detail =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : '未知错误';
      setErrorMessage(`「${stepName}」阶段失败：${detail}`);
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="手动上传合同送审"
      open={open}
      width={720}
      onCancel={handleClose}
      maskClosable={false}
      footer={
        <Space>
          <Button onClick={handleClose} disabled={submitting}>
            取消
          </Button>
          <Button
            type="primary"
            loading={submitting}
            onClick={() => void handleSubmit()}
          >
            提交送审
          </Button>
        </Space>
      }
    >
      {/* ---------------- 流程进度 ---------------- */}
      {submitting && (
        <div style={{ marginBottom: 16 }}>
          <Steps
            size="small"
            current={currentStep}
            items={STEP_ITEMS}
            status="process"
          />
          {currentStep === 0 && (
            <Progress
              percent={uploadPercent}
              size="small"
              style={{ marginTop: 12 }}
              status="active"
            />
          )}
        </div>
      )}

      {/* ---------------- 失败提示 ---------------- */}
      {errorMessage && (
        <Alert
          type="error"
          showIcon
          message="送审失败"
          description={errorMessage}
          style={{ marginBottom: 16 }}
          closable
          onClose={() => setErrorMessage(null)}
        />
      )}

      {/* ---------------- 文件选择（紧凑版拖拽区） ----------------
          说明：antd Dragger 默认 height:100% 的高度链在弹窗中被浏览器拉伸
          （实测 622px），样式修正见 upload-compact.css（height:auto 打断）。
          此处通过 rootClassName 限定作用范围，仅本弹窗的拖拽区生效。 */}
      <Dragger
        rootClassName="compact-upload-dragger"
        multiple={false}
        maxCount={1}
        fileList={fileList}
        disabled={submitting}
        accept={ALLOWED_UPLOAD_EXTS.join(',')}
        beforeUpload={(file) => {
          const valid = validateFile(file, (msg) => message.error(msg));
          if (!valid) return Upload.LIST_IGNORE;

          // 说明一：阻止 antd 自动上传，改由提交时统一走业务上传接口，
          //   便于在同一个流程里串联创建任务与触发解析。
          // 说明二：这里【只做校验】，不调用 setFileList。
          //   受控模式下 antd 的 onBatchStart 内部已经通过 onInternalChange
          //   把文件写回列表；如果这里再 setFileList 一次，由于 React 18 的
          //   flushSync 同步刷新，antd 内部的 mergedFileList 会先于本组件的
          //   fileList state 更新，随后 onBatchStart 里 "先合并再 updateFileList"
          //   的流程会看到仍然为空的旧列表，最终触发两次 onChange
          //   （先 1 个再 0 个），表现为「选中文件后列表反而空掉、点了没反应」。
          return false;
        }}
        // 上传列表的唯一数据源：始终以 antd 回调给我们的 fileList 为准。
        //
        // 说明一：slice(-1) 是必需而不是冗余。maxCount=1 时 antd 内部虽会
        //   slice(-1)，但受控模式下它下发给 onChange 的列表仍可能是「旧项 + 新项」
        //   两条（旧项要等离场动画结束才被移除）。此处显式取最后一项，
        //   可保证本组件的 state 恒为「仅当前文件」，避免旧项被当成已选文件。
        // 说明二：补充 originFileObj，保证提交时能取到真实 File 走 multipart 上传。
        onChange={({ fileList: nextFileList }) => {
          setFileList(
            nextFileList.slice(-1).map((item) => ({
              ...item,
              originFileObj: item.originFileObj ?? (item as UploadFile).originFileObj,
            })),
          );
          // 重新选择文件时清掉上一次的失败提示，避免旧错误误导使用者
          if (nextFileList.length > 0) setErrorMessage(null);
        }}
        onRemove={() => {
          setFileList([]);
          return true;
        }}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽合同文件到此处上传</p>
        <p className="ant-upload-hint" style={{ fontSize: 12 }}>
          支持 PDF / Word / 图片扫描件，单个不超过 {formatFileSize(MAX_UPLOAD_SIZE)}
          。提交后系统将自动识别合同信息并启动审查。
        </p>
      </Dragger>
    </Modal>
  );
}
