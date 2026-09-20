// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/layouts/MainLayout.tsx
/**
 * 主布局（侧边导航 + 顶栏 + 内容区）
 *
 * 【文件职责】
 *   为三个核心页面提供统一的骨架：左侧导航、顶部信息栏、主内容出口。
 *
 * 【导航结构设计】
 *   导航项严格对应业务闭环的三个阶段：
 *     1. 审查大盘   —— 任务总览、批量调度、阻塞处理
 *     2. 审查工作台 —— 单任务深度复核（核心作业面）
 *     3. 规则与模板 —— 规则资产与示范条款库维护
 *
 *   【菜单按角色过滤】
 *     菜单项必须与路由守卫的口径保持一致：守卫会拦截的页面，
 *     菜单里就不该出现。否则用户点进去被弹回来，会误以为系统出故障。
 *     为此菜单的可见性直接复用 constants.ROUTE_ALLOWED_ROLES 这张表，
 *     保证"菜单能看到的"与"路由能进去的"永远同源。
 *
 * 【权责提示的常驻展示】
 *   顶部右侧常驻"规则引擎终裁"标识，向使用者持续声明：
 *   风险等级与审查结论由规则引擎产出，大模型仅提供辅助建议。
 *   这是产品合规性的一部分，不是装饰。
 *
 * 【用户信息区】
 *   右上角展示当前登录用户的显示名、账号与角色标签——
 *   全部来自 authStore 中后端下发的真实数据，不硬编码任何岗位名。
 *   点击展开菜单可退出登录（清空令牌并跳转登录页）。
 *
 * 【响应式适配说明（移动端）】
 *   桌面端使用固定宽度的左导航 Sider；视口 < md（手机）时改为：
 *     - 左导航收起为顶部汉堡按钮触发的 Drawer 抽屉（覆盖式，不挤压内容）；
 *     - 顶栏隐藏较宽的权责声明 Tag，避免横向溢出；
 *     - 顶栏内边距收窄，给触控目标留出空间。
 *   这样手机上既能完整访问全部导航，又不会被固定的 224px 侧栏挤占阅读区。
 */

import { useMemo, useState } from 'react';
import {
  App,
  Avatar,
  Button,
  Drawer,
  Dropdown,
  Layout,
  Menu,
  Modal,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { MenuProps } from 'antd';
import {
  AuditOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  MenuOutlined,
  ProfileOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';

import {
  ROLE_HOME_PATH,
  ROUTE_ALLOWED_ROLES,
  ROUTE_PATH,
  USER_ROLE_LABEL,
  USER_ROLE_TAG_COLOR,
} from '@/constants';
import { useAuthStore } from '@/stores/authStore';
import { useResponsive } from '@/hooks/useResponsive';
import NotificationCenter from '@/components/NotificationCenter';

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

/** 导航项定义 */
interface NavItem {
  /** 路由路径 */
  key: string;
  /** 菜单标题 */
  label: string;
  /** 菜单图标 */
  icon: React.ReactNode;
  /** 悬浮说明 */
  hint: string;
}

/** 导航清单（顺序即业务流转顺序） */
const NAV_ITEMS: NavItem[] = [
  {
    key: ROUTE_PATH.DASHBOARD,
    label: '合同审查大盘',
    icon: <ProfileOutlined />,
    hint: '待审合同总览、批量调度与阻塞任务处理',
  },
  {
    key: ROUTE_PATH.WORKBENCH,
    label: '智能审查工作台',
    icon: <FileSearchOutlined />,
    hint: '条款级风险复核与审批意见回写',
  },
  {
    key: ROUTE_PATH.RULES,
    label: '合规规则与模板',
    icon: <SafetyCertificateOutlined />,
    hint: '按合同类型维护审查规则与示范条款库',
  },
];

export default function MainLayout(): React.ReactElement {
  const navigate = useNavigate();
  const location = useLocation();
  const { message: messageApi } = App.useApp();

  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const loggingOut = useAuthStore((state) => state.loggingOut);
  /** 退出登录确认弹窗显隐 */
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  /** 移动端抽屉式导航显隐 */
  const [drawerOpen, setDrawerOpen] = useState(false);
  /** 当前视口是否处于手机尺寸 */
  const { isMobile } = useResponsive();

  /**
   * 按角色过滤后的可见导航项
   *
   * 说明：
   *   复用 ROUTE_ALLOWED_ROLES 与守卫同源，避免"菜单显示但进不去"。
   *   未登记角色限制的路径视为任意登录用户可见。
   */
  const visibleNavItems = useMemo<NavItem[]>(() => {
    const role = user?.role;
    if (!role) return [];

    return NAV_ITEMS.filter((item) => {
      const allowed = ROUTE_ALLOWED_ROLES[item.key];
      // 无角色限制 → 所有人可见；有则必须命中
      return !allowed || allowed.includes(role);
    });
  }, [user?.role]);

  /**
   * 派生当前选中的菜单项
   *
   * 说明：工作台页面通过 /workbench/:taskNo 携带任务编号，
   * 因此需要用前缀匹配而非全等匹配，否则进入详情后菜单会失去高亮。
   */
  const selectedKeys = useMemo<string[]>(() => {
    const matched = visibleNavItems.find((item) =>
      location.pathname.startsWith(item.key),
    );
    // 匹配不到时回落到该角色的默认首页，保证根路径下菜单有高亮
    return [matched?.key ?? ROLE_HOME_PATH[user?.role ?? 'business_user']];
  }, [location.pathname, visibleNavItems, user?.role]);

  /** 当前页面标题 */
  const currentTitle = useMemo<string>(() => {
    const matched = NAV_ITEMS.find((item) =>
      location.pathname.startsWith(item.key),
    );
    return matched?.label ?? '合同审查';
  }, [location.pathname]);

  /**
   * 执行退出登录
   *
   * 返回：
   *   Promise<void>
   *
   * 说明：
   *   先关闭确认弹窗，再调 store 的 logout（内部已"尽力而为"调后端），
   *   最后由本函数负责跳转——跳转放在视图层而非 store，
   *   避免 store 依赖路由能力（store 应是纯数据与副作用层）。
   */
  const handleLogout = async (): Promise<void> => {
    setLogoutConfirmOpen(false);
    await logout();
    messageApi.success('已安全退出登录');
    navigate(ROUTE_PATH.LOGIN, { replace: true });
  };

  /** 用户下拉菜单项 */
  const userMenuItems = useMemo<MenuProps['items']>(
    () => [
      {
        key: 'profile',
        icon: <UserOutlined />,
        label: (
          <div style={{ lineHeight: 1.5, padding: '2px 0' }}>
            <div style={{ fontWeight: 600 }}>{user?.display_name ?? '未登录'}</div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {user?.username ?? ''}
              {user?.department ? ` · ${user.department}` : ''}
            </Text>
          </div>
        ),
        disabled: true,
      },
      { type: 'divider' },
      {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: '退出登录',
        danger: true,
      },
    ],
    [user?.display_name, user?.username, user?.department],
  );

  /**
   * 用户下拉菜单点击处理
   *
   * 参数：
   *   info: 菜单点击事件信息
   *
   * 返回：
   *   无
   */
  const handleUserMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'logout') {
      // 二次确认：退出会丢失当前页面的未保存状态，不该一键直退
      setLogoutConfirmOpen(true);
    }
  };

  /** 品牌区（桌面 Sider 与移动 Drawer 共用） */
  const renderBrand = (): React.ReactNode => (
    <div
      style={{
        height: 64,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '0 20px',
        color: '#fff',
      }}
    >
      <AuditOutlined style={{ fontSize: 20 }} />
      <div style={{ lineHeight: 1.2 }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>明契智审</div>
        <div style={{ fontSize: 11, opacity: 0.55 }}>Contract Review</div>
      </div>
    </div>
  );

  /**
   * 导航菜单（桌面 Sider 与移动 Drawer 共用）
   *
   * 参数：
   *   onItemClick: 点击菜单项后的额外回调（移动端用于点击后关闭抽屉）
   */
  const renderNavMenu = (onItemClick?: () => void): React.ReactNode => (
    <Menu
      theme="dark"
      mode="inline"
      selectedKeys={selectedKeys}
      onClick={({ key }) => {
        navigate(key);
        onItemClick?.();
      }}
      items={visibleNavItems.map((item) => ({
        key: item.key,
        icon: item.icon,
        label: (
          <Tooltip title={item.hint} placement="right">
            <span>{item.label}</span>
          </Tooltip>
        ),
      }))}
    />
  );

  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* ---------------- 桌面端：左侧固定导航 ---------------- */}
      {!isMobile && (
        <Sider
          theme="dark"
          width={224}
          style={{ position: 'sticky', top: 0, height: '100vh' }}
        >
          {renderBrand()}
          {renderNavMenu()}
        </Sider>
      )}

      {/* ---------------- 移动端：抽屉式导航 ---------------- */}
      {isMobile && (
        <Drawer
          placement="left"
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width={224}
          closable={false}
          styles={{
            body: { padding: 0, background: '#001529' },
            header: { display: 'none' },
          }}
        >
          {renderBrand()}
          {renderNavMenu(() => setDrawerOpen(false))}
        </Drawer>
      )}

      <Layout>
        {/* ---------------- 顶栏 ---------------- */}
        <Header
          style={{
            background: '#fff',
            // 手机端内边距收窄，给触控目标与汉堡按钮留空间
            padding: isMobile ? '0 12px' : '0 24px',
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            position: 'sticky',
            top: 0,
            zIndex: 10,
          }}
        >
          <Space size={8}>
            {/* 手机端：汉堡按钮唤起抽屉导航 */}
            {isMobile && (
              <Button
                type="text"
                aria-label="打开导航菜单"
                icon={<MenuOutlined />}
                onClick={() => setDrawerOpen(true)}
                style={{ fontSize: 18 }}
              />
            )}
            <Text strong style={{ fontSize: isMobile ? 15 : 16 }}>
              {currentTitle}
            </Text>
          </Space>

          <Space size={12}>
            {/* 站内消息中心（右上角铃铛，未读红点） */}
            <NotificationCenter />

            {/* 权责声明：常驻提示，非装饰；手机端隐藏以免横向溢出 */}
            {!isMobile && (
              <Tooltip title="风险等级定级、违规判定与审查结论均由规则引擎终裁；大模型仅提供条款语义解析与修改建议草稿。">
                <Tag icon={<SafetyCertificateOutlined />} color="blue">
                  规则引擎终裁 · 大模型仅辅助
                </Tag>
              </Tooltip>
            )}

            {/* 当前登录用户：显示名 + 账号 + 角色标签（点击展开退出登录） */}
            <Dropdown
              menu={{ items: userMenuItems, onClick: handleUserMenuClick }}
              trigger={['click']}
              placement="bottomRight"
              disabled={loggingOut}
            >
              <Space
                size={8}
                style={{
                  cursor: 'pointer',
                  padding: '4px 8px',
                  borderRadius: 6,
                  transition: 'background 0.2s',
                }}
              >
                <Avatar
                  size={28}
                  style={{
                    background:
                      'linear-gradient(135deg, #1677ff, #0b5ed7)',
                    flexShrink: 0,
                  }}
                  icon={<UserOutlined />}
                />
                <Space size={6}>
                  <Text style={{ fontSize: 13 }}>
                    {user?.display_name ?? '未登录'}
                  </Text>
                  {user && (
                    <Tag
                      color={USER_ROLE_TAG_COLOR[user.role]}
                      style={{ marginInlineEnd: 0, fontSize: 11 }}
                    >
                      {USER_ROLE_LABEL[user.role] ?? user.role_label}
                    </Tag>
                  )}
                </Space>
              </Space>
            </Dropdown>
          </Space>
        </Header>

        {/* ---------------- 内容区 ---------------- */}
        <Content
          style={{
            padding: isMobile ? 12 : 20,
            background: '#f5f5f5',
            overflow: 'auto',
          }}
        >
          <Outlet />
        </Content>
      </Layout>

      {/* ---------------- 退出登录二次确认 ---------------- */}
      <Modal
        title="确认退出登录？"
        open={logoutConfirmOpen}
        onOk={handleLogout}
        onCancel={() => setLogoutConfirmOpen(false)}
        okText="退出登录"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: loggingOut }}
        maskClosable={!loggingOut}
        closable={!loggingOut}
        centered
      >
        <Space direction="vertical" size={6}>
          <Text>退出后需要重新输入账号密码才能继续使用。</Text>
          {user && (
            <Text type="secondary" style={{ fontSize: 13 }}>
              当前账号：{user.username}（
              {USER_ROLE_LABEL[user.role] ?? user.role_label}）
            </Text>
          )}
          <Text type="secondary" style={{ fontSize: 13 }}>
            服务端会同时撤销该账号已签发的全部令牌。
          </Text>
        </Space>
      </Modal>
    </Layout>
  );
}
