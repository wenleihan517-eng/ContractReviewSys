// D:/ContractReviewSys-合同审批智能审查项目/frontend/src/components/NotificationCenter.tsx
/**
 * 站内消息中心（右上角铃铛）
 *
 * 【文件职责】
 *   展示当前用户可见的站内通知（审查完成 / 审查失败 / 系统告警），
 *   未读时显示小红点；点击通知跳转到对应合同工作台并自动标记已读。
 *
 * 【分级与高亮】
 *   - 失败/系统告警类（level=critical）：左侧红条高亮，优先展示
 *   - 完成类（level=info）：普通展示
 */

import { useEffect, useState } from 'react';
import { Badge, Button, Empty, Popover, Space, Typography } from 'antd';
import { BellOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

import { notificationApi } from '@/api/endpoints';
import type { NotificationItem } from '@/types';

const { Text } = Typography;

/** 通知图标色：critical 红色，其余默认蓝 */
const LEVEL_COLOR: Record<string, string> = {
  critical: '#ff4d4f',
  info: '#1677ff',
};

export default function NotificationCenter(): React.ReactElement {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<NotificationItem[]>([]);

  /** 拉取未读数与列表 */
  const refresh = async (): Promise<void> => {
    try {
      const [cnt, list] = await Promise.all([
        notificationApi.unreadCount(),
        notificationApi.list(1, 20),
      ]);
      setUnread(cnt.unread_count ?? 0);
      setItems(list.items ?? []);
    } catch {
      // 通知加载失败不打扰用户（静默），下次打开/轮询再试
    }
  };

  // 挂载时加载 + 每 30 秒轮询未读（轻量感知，不轰炸）
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void notificationApi.unreadCount().then((r) => {
        setUnread(r.unread_count ?? 0);
      }).catch(() => undefined);
    }, 30000);
    return () => window.clearInterval(timer);
  }, []);

  /** 打开面板时刷新完整列表 */
  const handleOpenChange = (next: boolean): void => {
    setOpen(next);
    if (next) {
      void refresh();
    }
  };

  /** 点击某条通知：跳转工作台 + 标记已读 */
  const handleClick = async (item: NotificationItem): Promise<void> => {
    if (!item.read) {
      // 乐观更新：本地先置已读，接口失败时下次刷新会纠正
      setUnread((n) => Math.max(0, n - 1));
      setItems((list) =>
        list.map((x) => (x.notify_id === item.notify_id ? { ...x, read: true } : x)),
      );
      try {
        await notificationApi.markRead(item.notify_id);
      } catch {
        // 忽略单条标记失败
      }
    }
    setOpen(false);
    if (item.task_no) {
      navigate(`/workbench/${item.task_no}`);
    }
  };

  /** 全部已读 */
  const handleReadAll = async (): Promise<void> => {
    try {
      await notificationApi.markAllRead();
      setUnread(0);
      setItems((list) => list.map((x) => ({ ...x, read: true })));
    } catch {
      // 忽略
    }
  };

  const content = (
    <div style={{ width: 360, maxHeight: 480, overflow: 'auto' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '8px 4px',
          borderBottom: '1px solid #f0f0f0',
        }}
      >
        <Text strong>系统通知</Text>
        <Button type="link" size="small" onClick={() => void handleReadAll()}>
          全部已读
        </Button>
      </div>

      {items.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无通知"
          style={{ padding: '24px 0' }}
        />
      ) : (
        items.map((item) => (
          <div
            key={item.notify_id}
            onClick={() => void handleClick(item)}
            style={{
              padding: '10px 8px',
              borderBottom: '1px solid #f5f5f5',
              cursor: 'pointer',
              display: 'flex',
              gap: 8,
              alignItems: 'flex-start',
              background: item.read ? 'transparent' : '#f0f7ff',
            }}
          >
            {/* 左侧级别色条 */}
            <span
              style={{
                width: 3,
                alignSelf: 'stretch',
                borderRadius: 2,
                flexShrink: 0,
                background: LEVEL_COLOR[item.level] ?? '#1677ff',
              }}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
                <Text
                  strong
                  style={{ fontSize: 13, color: item.level === 'critical' ? '#ff4d4f' : undefined }}
                >
                  {item.title}
                </Text>
                {!item.read && (
                  <Badge color="#ff4d4f" style={{ flexShrink: 0 }} />
                )}
              </Space>
              <div
                style={{
                  fontSize: 12,
                  color: '#666',
                  whiteSpace: 'pre-line',
                  maxHeight: 72,
                  overflow: 'hidden',
                }}
              >
                {item.content}
              </div>
            </div>
          </div>
        ))
      )}
    </div>
  );

  return (
    <Popover
      content={content}
      trigger="click"
      open={open}
      onOpenChange={handleOpenChange}
      placement="bottomRight"
      title={null}
    >
      <Badge count={unread} size="small" offset={[-2, 2]}>
        <Button type="text" icon={<BellOutlined style={{ fontSize: 16 }} />} />
      </Badge>
    </Popover>
  );
}
