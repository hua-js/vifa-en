# M3 NocoBase 普通 iframe 配置说明

当前生产模式为公开只读。NocoBase 只通过 URL 嵌入 Node-RED 页面，不传递用户 Token：

```text
https://opdash.lvkpower.com/ett
```

## 安全边界

- `/ett` 与 `/energy-forecast-api` 对任何能访问 OPDash 域名的人公开；
- 页面只能读取两个电站的预测看板数据；
- 训练、写表和管理接口不通过该 Flow 暴露；
- Dashboard 四表只读 Key 只保存在 `/etc/vifa-m3/dashboard.env`，不会进入 iframe、URL、
  Node-RED Flow 或浏览器；
- 不使用 Header、query Token、固定管理员 Token、JavaScript Block或 postMessage。

## 人工配置

1. 登录 `https://ems.lvkpower.com`；
2. 打开目标页面并进入界面配置模式；
3. 新增普通 iframe 区块；
4. 标题填写“场站未来能耗预测”；
5. URL 固定填写 `https://opdash.lvkpower.com/ett`；
6. 不填写 Header 或 URL 参数；
7. 保存并退出配置模式。

## 验收

- 直接访问 `https://opdash.lvkpower.com/ett` 返回页面，不再显示“登录状态无效”；
- `/energy-forecast-api` 返回 `status: ok`；
- 页面区分 1#、2# 电站；
- 每站分别显示总负荷和 SOC；
- 展示最近 24 小时实绩与未来 24 小时预测；
- URL、页面源码、Node-RED Flow 和日志中没有四表只读 Key。

## 回退

停用 NocoBase iframe 区块和 M3 Node-RED Flow。不要删除 M3 四表、预测历史、env 或 Token，
不要重启 Node-RED。

