# 百炼首次开通引导（agent 陪跑手册）

本文档给**调用方 agent**看：当用户从未注册/开通阿里云百炼（Model Studio）时，按此引导用户完成开通，关键权限步骤由人完成，其余由 agent 代劳。已有 key 的用户跳过本文档，直接跑 `verify-key` 验证。

## 防漂移（每次引导前必做）

平台界面会改版。**先 WebFetch 以下官方文档确认当前流程**，本文档只提供骨架——与线上说明冲突时，以官方文档为准：

1. https://help.aliyun.com/zh/model-studio/get-api-key （获取 API Key）
2. https://help.aliyun.com/zh/model-studio/new-free-quota （新人免费额度）
3. https://help.aliyun.com/zh/model-studio/paraformer-v2 （录音文件识别模型）
4. https://help.aliyun.com/zh/model-studio/paraformer-realtime-v2 （实时识别模型）

若 WebFetch 失败（网络/代理），如实告知用户并以本文档骨架继续，注明信息可能过时。

## 人机分工

| 步骤 | 谁 | 内容 |
|---|---|---|
| 注册阿里云账号 | **人** | 手机号 + 验证码（涉及短信验证，agent 无法代做） |
| 开通百炼服务 | **人** | 首次进入百炼控制台，同意协议即开通（免费额度自动发放） |
| 开通模型权限 | **人** | 在百炼控制台业务空间内，**逐个"开通" paraformer-v2 与 paraformer-realtime-v2**（ASR 类模型必须逐模型开通后才能调用并消耗免费额度——最易漏的一步） |
| 创建 API Key | **人** | 控制台 API-Key 页点击创建并复制（地域必须选**华北2（北京）**，Key 永不过期） |
| 写入配置 | agent | 把 key 写入 `skill/.env` 的 `DASHSCOPE_API_KEY=`（该文件已 gitignore，不入库） |
| 验证与汇报 | agent | 跑 `verify-key`，解读结果并告知可用性状态 |

## 步骤清单

1. **【agent】** WebFetch 上述 4 个文档，摘出当前注册/开通流程要点。
2. **【agent】** 给用户打开百炼入口：https://bailian.console.aliyun.com/ （或让用户搜索"阿里云百炼"），说明即将发生什么（注册 → 开通 → 建 key → 开通模型）。
3. **【人】** 注册阿里云账号并完成登录（已有阿里云账号可跳过）。
4. **【人】** 首次进入百炼控制台，同意服务协议完成开通。
   - 判据：能看到模型广场/业务空间界面。
5. **【人】** **开通两个模型**：在模型广场分别找到 paraformer-v2 与 paraformer-realtime-v2，点"开通"（业务空间内逐模型授权）。
   - 判据：两个模型在控制台显示"已开通"。
   - 官方事实：免费额度在**首次开通百炼时自动发放**，无需手动领取；仅**华北2（北京）**地域模型有免费额度；免费额度通常无需实名认证即可使用，但建议完成实名以便额度用尽后可按量付费。
6. **【人】** 创建 API Key：控制台左侧 API-Key 页 →（确认地域为北京）→ 创建 → 复制。
7. **【agent】** 把 key 写入 `skill/.env`：追加/替换行 `DASHSCOPE_API_KEY=sk-…`。**绝不回显 key 内容，不写入对话或任何产物文件。**
8. **【agent】** 验证并汇报：

```bash
<skill>/scripts/video_batch_cli.py verify-key
```

结果解读（退出码 + JSON）：

| status / 退出码 | 含义 | 下一步 |
|---|---|---|
| `ok` / 0 | 两个模型的 getPolicy 均鉴权通过，key 有效 | 可用。告知用户：key 有效；是否已逐模型开通要等首次真实转写才可知 |
| `invalid` / 2 | key 被拒（401/403） | 检查 key 是否复制完整、地域是否北京；必要时删除重建 |
| `error` / 1 | 网络或其他错误 | 看各 model 的 http_status/detail；检查网络（系统代理）后重试 |

若用户同意消耗几秒免费额度做端到端确认，可用一段 5–10 秒语音跑 `run --file <语音文件>`：成功=全链路可用；报"未开通/Model not exists"类错误=第 5 步没开通成功，回控制台补点。

## 安全

- key 只进 `skill/.env`（已 gitignore）；不回显、不进 summary、不进对话正文。
- 引导中 agent 不代替用户点击任何授权/付费动作；仅指路。
