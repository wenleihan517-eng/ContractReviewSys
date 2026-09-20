// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/pages/LoginPage.tsx
/**
 * 登录页
 *
 * 【文件职责】
 *   1. 账号 / 密码 / 验证码三个输入框与提交按钮
 *   2. 表单校验：非空 + 格式（账号以字母开头、密码长度、验证码一致性）
 *   3. 错误分类提示：账号密码错误 / 账号锁定或禁用 / 网络异常 / 登录失效
 *   4. 登录成功后按角色跳转到各自默认首页（管理员→规则页、法务→工作台、经办人→大盘）
 *
 * 【演示版验证码说明（务必明确边界）】
 *   本项目的验证码是**纯前端演示实现**：在 Canvas 上随机绘制 4 个字符，
 *   校验也在前端本地完成。它**不具备任何安全价值**——攻击者可以直接读
 *   React state 或直接调用后端登录接口绕过。保留它的目的是演示
 *   "登录表单含验证码字段"这一完整交互链路。
 *   生产环境必须改为服务端签发 + 服务端校验（含一次性失效、有效期）。
 *
 * 【关于错误提示的分级策略】
 *   后端对"账号不存在"与"密码错误"返回**完全相同**的 60001 与文案，
 *   这是有意的账号枚举防御。前端必须原样展示后端文案，
 *   绝不能自作聪明地按 username 是否存在给出不同提示——
 *   那会把后端的防御措施从界面上重新打穿。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Space,
  Typography,
} from 'antd';
import {
  AuditOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';

import { ApiError, ERROR_CODE } from '@/api/client';
import {
  CAPTCHA_CONFIG,
  LOGIN_SUCCESS_DELAY_MS,
  NAV_REASON,
  type NavReason,
} from '@/constants';
import { useAuthStore } from '@/stores/authStore';

const { Title, Text, Paragraph } = Typography;

// ======================================================================
// 一、演示版验证码
// ======================================================================

/**
 * 随机生成验证码字符
 *
 * 返回：
 *   长度为 CAPTCHA_CONFIG.LENGTH 的验证码字符串
 *
 * 说明：
 *   字符集已剔除 0/O、1/I/l 等肉眼易混淆字符，
 *   避免用户"明明看对了却输错"的挫败感。
 */
function generateCaptchaText(): string {
  const { CHARSET, LENGTH } = CAPTCHA_CONFIG;
  let text = '';
  for (let i = 0; i < LENGTH; i += 1) {
    text += CHARSET.charAt(Math.floor(Math.random() * CHARSET.length));
  }
  return text;
}

/**
 * 在 Canvas 上绘制验证码
 *
 * 参数：
 *   canvas: 目标画布元素
 *   text: 待绘制文本
 *
 * 返回：
 *   无
 *
 * 说明：
 *   加入旋转与干扰线是为了让验证码"看起来像真的"，
 *   属于演示观感，不构成任何防 OCR 能力。
 */
function drawCaptcha(canvas: HTMLCanvasElement, text: string): void {
  const { WIDTH, HEIGHT, ROTATE_RANGE } = CAPTCHA_CONFIG;
  // 按设备像素比放大绘制，避免高分屏上文字发虚
  const dpr = window.devicePixelRatio || 1;
  canvas.width = WIDTH * dpr;
  canvas.height = HEIGHT * dpr;
  canvas.style.width = `${WIDTH}px`;
  canvas.style.height = `${HEIGHT}px`;

  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, WIDTH, HEIGHT);

  // 底色
  ctx.fillStyle = '#f2f5fa';
  ctx.fillRect(0, 0, WIDTH, HEIGHT);

  // 干扰线（3 条）
  for (let i = 0; i < 3; i += 1) {
    ctx.strokeStyle = `rgba(22,119,255,${0.15 + Math.random() * 0.2})`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.random() * WIDTH, Math.random() * HEIGHT);
    ctx.lineTo(Math.random() * WIDTH, Math.random() * HEIGHT);
    ctx.stroke();
  }

  // 逐字符绘制（带随机旋转与基线偏移）
  const charWidth = WIDTH / (text.length + 1);
  text.split('').forEach((char, index) => {
    ctx.save();
    const x = charWidth * (index + 0.7);
    const y = HEIGHT / 2 + (Math.random() - 0.5) * 6;
    ctx.translate(x, y);
    ctx.rotate((Math.random() - 0.5) * 2 * ROTATE_RANGE);
    ctx.font = 'bold 21px "SF Mono", Consolas, monospace';
    ctx.fillStyle = ['#1677ff', '#0b5ed7', '#3b82f6', '#1d4ed8'][
      Math.floor(Math.random() * 4)
    ];
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(char, 0, 0);
    ctx.restore();
  });

  // 干扰点
  for (let i = 0; i < 24; i += 1) {
    ctx.fillStyle = `rgba(22,119,255,${Math.random() * 0.5})`;
    ctx.fillRect(Math.random() * WIDTH, Math.random() * HEIGHT, 1.4, 1.4);
  }
}

// ======================================================================
// 二、登录表单模型
// ======================================================================

interface LoginFormValues {
  username: string;
  password: string;
  captcha: string;
}

/** "记住账号"的本地存储键 */
const REMEMBERED_USERNAME_KEY = 'cr_remembered_username';

/**
 * 演示账号快捷登录配置
 *
 * 说明：密码统一为 123456；验证码填 "demo"（纯前端演示校验，
 * 点击按钮时同步把验证码比对值置为 demo 以绕过本地校验）。
 */
const QUICK_LOGIN_ACCOUNTS: ReadonlyArray<{
  label: string;
  username: string;
}> = [
  { label: '业务经办人', username: 'user_business' },
  { label: '法务审查人', username: 'user_law' },
  { label: '系统管理员', username: 'user_admin' },
];

/**
 * 读取上次记住的账号
 *
 * 说明：只记住**账号**，绝不记住密码。
 * 把密码持久化到 localStorage 是最常见也是最严重的登录页缺陷之一，
 * 任何同源脚本都能直接读走明文密码。
 *
 * 返回：
 *   上次记住的账号；无记录时返回空字符串
 */
function readRememberedUsername(): string {
  try {
    return window.localStorage.getItem(REMEMBERED_USERNAME_KEY) ?? '';
  } catch {
    return '';
  }
}

/**
 * 写入/清除记住的账号
 *
 * 参数：
 *   username: 账号；传空字符串表示清除
 *
 * 返回：
 *   无
 */
function writeRememberedUsername(username: string): void {
  try {
    if (username) {
      window.localStorage.setItem(REMEMBERED_USERNAME_KEY, username);
    } else {
      window.localStorage.removeItem(REMEMBERED_USERNAME_KEY);
    }
  } catch {
    // 存储不可用时静默降级：仅本次会话不回填账号
  }
}

// ======================================================================
// 三、页面组件
// ======================================================================

export default function LoginPage(): React.ReactElement {
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = App.useApp();
  const [form] = Form.useForm<LoginFormValues>();

  const login = useAuthStore((state) => state.login);
  const loggingIn = useAuthStore((state) => state.loggingIn);

  /** 正确验证码（保存在 state 中，刷新时重新生成） */
  const [captchaText, setCaptchaText] = useState<string>(() =>
    generateCaptchaText(),
  );
  /** 画布引用 */
  const canvasRef = useRef<HTMLCanvasElement>(null);
  /** 登录按钮引用（快捷填充后聚焦，便于一键回车提交） */
  const submitButtonRef = useRef<HTMLButtonElement | null>(null);
  /** 表单级错误提示（账号密码错误等） */
  const [formError, setFormError] = useState<string | null>(null);

  /** 上次记住的账号（仅账号，不含密码） */
  const rememberedUsername = useMemo(() => readRememberedUsername(), []);
  /** 是否记住账号 */
  const [rememberMe, setRememberMe] = useState(() => Boolean(rememberedUsername));

  /**
   * 解析跳转原因与来源路径
   *
   * 说明：
   *   reason=unauthenticated —— 由 axios 401 或路由守卫踢回
   *   from=xxx                —— 登录成功后回跳的原页面
   *   限制 from 必须以 "/" 开头，防止被构造成外部地址造成开放重定向。
   */
  const { reason, fromPath } = useMemo<{
    reason: NavReason | null;
    fromPath: string | null;
  }>(() => {
    const params = new URLSearchParams(location.search);
    const rawReason = params.get('reason');
    const rawFrom = params.get('from');

    const parsedReason =
      rawReason === NAV_REASON.UNAUTHENTICATED ||
      rawReason === NAV_REASON.FORBIDDEN
        ? (rawReason as NavReason)
        : null;

    // 开放重定向防御：只接受站内相对路径
    const safeFrom =
      rawFrom && rawFrom.startsWith('/') && !rawFrom.startsWith('//')
        ? rawFrom
        : null;

    return { reason: parsedReason, fromPath: safeFrom };
  }, [location.search]);

  /**
   * 重绘验证码
   *
   * 说明：切换验证码时必须同时重置表单中的 captcha 字段，
   * 否则用户会拿旧值去比对新验证码，必然失败且不知为何。
   */
  const refreshCaptcha = useCallback((): void => {
    const next = generateCaptchaText();
    setCaptchaText(next);
    form.setFieldValue('captcha', '');
    if (canvasRef.current) {
      drawCaptcha(canvasRef.current, next);
    }
  }, [form]);

  // 首次挂载与验证码变更时重绘
  useEffect(() => {
    if (canvasRef.current) {
      drawCaptcha(canvasRef.current, captchaText);
    }
  }, [captchaText]);

  /**
   * 快捷身份选择：一键回填账号/密码/验证码
   *
   * 参数：
   *   username: 目标演示账号
   *
   * 返回：
   *   无
   *
   * 说明：
   *   1. 密码保持星号隐藏（Input.Password 组件天然处理）
   *   2. 验证码填入 "demo"，并同步把 captchaText 置为 demo 并重绘，
   *      使前端本地比对通过（"绕过"仅针对演示版纯前端校验，
   *      后端登录接口不校验验证码，本改动不触碰后端）
   *   3. 回填后清除旧错误提示，并把焦点定位到登录按钮，
   *      用户按回车或点击即可提交登录
   */
  const handleQuickFill = (username: string): void => {
    const demoCaptcha = 'demo';
    form.setFieldsValue({
      username,
      password: '123456',
      captcha: demoCaptcha,
    });
    setCaptchaText(demoCaptcha);
    if (canvasRef.current) {
      drawCaptcha(canvasRef.current, demoCaptcha);
    }
    setFormError(null);
    submitButtonRef.current?.focus();
  };

  /**
   * 提交登录
   *
   * 参数：
   *   values: 表单值
   *
   * 返回：
   *   Promise<void>
   *
   * 说明：
   *   验证码在进入后端调用前做本地比对；比对失败立即抛错并刷新验证码，
   *   避免白跑一次网络往返（演示特性，生产应在服务端校验）。
   */
  const handleSubmit = async (values: LoginFormValues): Promise<void> => {
    setFormError(null);

    // 演示版验证码：本地比对（忽略大小写，提升演示容错）
    if (values.captcha.trim().toUpperCase() !== captchaText.toUpperCase()) {
      setFormError('验证码错误，请重新输入');
      refreshCaptcha();
      return;
    }

    try {
      const user = await login({
        username: values.username.trim(),
        password: values.password,
      });

      // 登录成功后才落"记住账号"，避免把拼错的账号也记住
      writeRememberedUsername(rememberMe ? values.username.trim() : '');

      message.success(`欢迎回来，${user.display_name}`);

      // 按角色解析落点：优先回跳来源页，否则走角色默认首页。
      // 延迟一小段再跳转，让成功提示有机会被看到。
      const target = fromPath ?? useAuthStore.getState().homePath();
      window.setTimeout(() => {
        navigate(target, { replace: true });
      }, LOGIN_SUCCESS_DELAY_MS);
    } catch (error) {
      // 统一把异常翻译为可读提示；同时刷新验证码，
      // 避免用户用同一验证码反复尝试
      setFormError(translateLoginError(error));
      refreshCaptcha();
    }
  };

  return (
    <div style={PAGE_STYLE}>
      <Card
        style={{ width: 420, boxShadow: '0 6px 28px rgba(15,35,95,0.12)' }}
        styles={{ body: { padding: '32px 36px 28px' } }}
      >
        {/* ---------------- 品牌区 ---------------- */}
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Space size={10} align="center">
            <div style={LOGO_STYLE}>
              <AuditOutlined style={{ fontSize: 22, color: '#fff' }} />
            </div>
            <div>
              <Title level={4} style={{ margin: 0 }}>
                明契智审
              </Title>
              <Text type="secondary" style={{ fontSize: 12 }}>
                Contract Review System
              </Text>
            </div>
          </Space>

          <Paragraph
            type="secondary"
            style={{ margin: '10px 0 18px', fontSize: 12.5 }}
          >
            风险定级与审查结论由规则引擎终裁，大模型仅提供辅助建议。
          </Paragraph>
        </Space>

        {/* ---------------- 跳转原因提示 ---------------- */}
        {reason === NAV_REASON.UNAUTHENTICATED && (
          <Alert
            type="warning"
            showIcon
            banner
            style={{ marginBottom: 16, borderRadius: 6 }}
            message="登录状态已失效，请重新登录"
          />
        )}
        {reason === NAV_REASON.FORBIDDEN && (
          <Alert
            type="error"
            showIcon
            banner
            style={{ marginBottom: 16, borderRadius: 6 }}
            message="当前账号权限不足，请使用有权限的账号登录"
          />
        )}

        {/* ---------------- 登录失败提示 ---------------- */}
        {formError && (
          <Alert
            type="error"
            showIcon
            closable
            style={{ marginBottom: 16, borderRadius: 6 }}
            message={formError}
            onClose={() => setFormError(null)}
          />
        )}

        {/* ---------------- 表单 ---------------- */}
        <Form<LoginFormValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          initialValues={{
            username: rememberedUsername,
            password: '',
            captcha: '',
          }}
          onFinish={handleSubmit}
          disabled={loggingIn}
        >
          <Form.Item
            name="username"
            label="账号"
            rules={[
              { required: true, message: '请输入账号' },
              { min: 3, max: 50, message: '账号长度为 3–50 个字符' },
              {
                pattern: /^[A-Za-z][A-Za-z0-9_]*$/,
                message: '账号以字母开头，仅可包含字母、数字与下划线',
              },
            ]}
          >
            <Input
              size="large"
              allowClear
              prefix={<UserOutlined style={{ color: '#bfbfbf' }} />}
              placeholder="请输入账号"
              autoComplete="username"
              onPressEnter={() => form.submit()}
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 1, max: 64, message: '密码长度为 1–64 个字符' },
            ]}
          >
            <Input.Password
              size="large"
              prefix={<LockOutlined style={{ color: '#bfbfbf' }} />}
              placeholder="请输入密码"
              autoComplete="current-password"
              onPressEnter={() => form.submit()}
            />
          </Form.Item>

          <Form.Item label="验证码" required style={{ marginBottom: 12 }}>
            <Space.Compact style={{ width: '100%' }}>
              <Form.Item
                name="captcha"
                noStyle
                rules={[
                  { required: true, message: '请输入验证码' },
                  {
                    len: CAPTCHA_CONFIG.LENGTH,
                    message: `请输入 ${CAPTCHA_CONFIG.LENGTH} 位验证码`,
                  },
                ]}
              >
                <Input
                  size="large"
                  prefix={
                    <SafetyCertificateOutlined style={{ color: '#bfbfbf' }} />
                  }
                  placeholder="请输入验证码"
                  style={{ flex: 1 }}
                  onPressEnter={() => form.submit()}
                />
              </Form.Item>

              <Button
                size="large"
                onClick={refreshCaptcha}
                title="点击刷新验证码"
                style={CAPTCHA_BUTTON_STYLE}
              >
                <canvas ref={canvasRef} />
              </Button>
            </Space.Compact>
          </Form.Item>

          <Form.Item style={{ marginBottom: 18 }}>
            <Space
              style={{ width: '100%', justifyContent: 'space-between' }}
              size={8}
            >
              <Checkbox
                checked={rememberMe}
                onChange={(event) => setRememberMe(event.target.checked)}
              >
                <Text style={{ fontSize: 13 }}>记住账号</Text>
              </Checkbox>
              <Text type="secondary" style={{ fontSize: 12 }}>
                演示系统 · 验证码仅作交互演示
              </Text>
            </Space>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button
              ref={submitButtonRef}
              type="primary"
              size="large"
              htmlType="submit"
              block
              loading={loggingIn}
            >
              登 录
            </Button>
          </Form.Item>
        </Form>

        {/* ---------------- 快捷身份选择（演示账号一键填充） ---------------- */}
        <div style={DEMO_BOX_STYLE}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            演示账号（密码均为 <Text code>123456</Text>），点击一键填充：
          </Text>
          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
            {QUICK_LOGIN_ACCOUNTS.map((account) => (
              <Button
                key={account.username}
                size="small"
                disabled={loggingIn}
                onClick={() => handleQuickFill(account.username)}
                style={QUICK_LOGIN_BUTTON_STYLE}
                title={`以「${account.label}」身份快捷登录`}
              >
                {account.label}
              </Button>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}

// ======================================================================
// 四、错误翻译
// ======================================================================

/**
 * 把登录异常翻译为面向用户的可读提示
 *
 * 参数：
 *   error: 捕获到的异常
 *
 * 返回：
 *   提示文案
 *
 * 说明：
 *   优先展示后端返回的 message——后端已做过账号枚举防御，
 *   其文案是"安全且准确"的；前端只对网络层异常做兜底翻译。
 */
function translateLoginError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case ERROR_CODE.CREDENTIALS_INVALID:
        // 原样使用后端文案：它同时覆盖"账号不存在"与"密码错误"，
        // 任何差异化展示都会破坏账号枚举防御
        return error.message || '账号或密码错误';
      case ERROR_CODE.ACCOUNT_DISABLED:
        return error.message || '账号已被禁用，请联系系统管理员';
      case ERROR_CODE.ACCOUNT_LOCKED:
        return error.message || '账号因连续登录失败已被锁定，请稍后重试';
      case ERROR_CODE.VALIDATION_ERROR:
        return error.message || '输入内容不合法，请检查后重试';
      case ERROR_CODE.SERVICE_UNAVAILABLE:
        return error.message || '网络异常，无法连接到服务器，请稍后重试';
      default:
        return error.message || '登录失败，请稍后重试';
    }
  }

  if (error instanceof Error) {
    return error.message || '登录失败，请稍后重试';
  }

  return '登录失败，请稍后重试';
}

// ======================================================================
// 五、样式（内联对象，避免额外 CSS 文件与主题变量失同步）
// ======================================================================

/** 页面容器样式：企业级登录页常用的居中卡片 + 轻量渐变背景 */
const PAGE_STYLE: React.CSSProperties = {
  minHeight: '100vh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 24,
  background:
    'linear-gradient(135deg, #eef4ff 0%, #f6f9ff 42%, #ffffff 100%)',
};

/** 品牌图标容器 */
const LOGO_STYLE: React.CSSProperties = {
  width: 44,
  height: 44,
  borderRadius: 10,
  background: 'linear-gradient(135deg, #1677ff, #0b5ed7)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  flexShrink: 0,
};

/** 验证码按钮：去掉默认内边距让 canvas 铺满 */
const CAPTCHA_BUTTON_STYLE: React.CSSProperties = {
  padding: 0,
  width: CAPTCHA_CONFIG.WIDTH,
  height: CAPTCHA_CONFIG.HEIGHT + 2,
  overflow: 'hidden',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};

/** 演示账号提示框 */
const DEMO_BOX_STYLE: React.CSSProperties = {
  marginTop: 20,
  padding: '10px 12px',
  background: '#fafafa',
  border: '1px solid #f0f0f0',
  borderRadius: 6,
  lineHeight: 1.7,
};

/** 快捷身份按钮：小而横向并排，沿用页面蓝色主色调，hover 时 antd 自动加深 */
const QUICK_LOGIN_BUTTON_STYLE: React.CSSProperties = {
  flex: 1,
  fontSize: 12,
  borderColor: '#1677ff',
  color: '#1677ff',
  whiteSpace: 'nowrap',
};
