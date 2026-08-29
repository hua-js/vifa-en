# M3 NocoBase 普通 iframe 配置说明

当前生产模式为公开查看和公开提交预测。NocoBase 只通过 URL 嵌入 Node-RED 页面，不传递用户 Token：

```text
https://opdash.lvkpower.com/ett
```

## 安全边界

- `/ett`、`/energy-forecast-api` 及自定义预测路由对任何能访问 OPDash 域名的人公开；
- 页面可以读取两个电站的预测看板数据并提交两站自定义预测任务；
- Node-RED 只允许固定路由、固定方法、两站白名单和受限预测参数，再通过 Worker UDS 发起任务；
- Worker 管理令牌只保存在服务端 `.worker-admin.token` 文件中，由 Node-RED Exec 读取，不会写入 Flow 或返回浏览器；
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
- 选择 7–90 天历史范围和 1–7 天预测时长后，“开始预测”可用；
- 自定义预测请求能够创建任务并返回结果；
- 页面区分 1#、2# 电站；
- 每站分别显示总负荷和 SOC；
- 展示最近 24 小时实绩与未来 24 小时预测；
- URL、页面源码、Node-RED Flow 和日志中没有四表只读 Key。

## 回退

停用 NocoBase iframe 区块和 M3 Node-RED Flow。不要删除 M3 四表、预测历史、env 或 Token，
不要重启 Node-RED。
