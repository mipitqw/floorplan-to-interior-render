# Seedream v5.5 生图接口参考（腾讯云 TokenHub）

> 实测时间 2026-11-19。参数表来自官方文档；"实测"标记的条目是本地实跑结论。

## 1. 端点与鉴权

```
POST https://tokenhub.tencentmaas.com/v1/wand/si-image/generation
Authorization: Bearer <TOKENHUB_API_KEY>
Content-Type: application/json
```

- Key 在 TokenHub 控制台创建：https://console.cloud.tencent.com/tokenhub/apikey
- 同步接口：一次请求返回图片地址，不需要轮询任务。
- 脚本**不内置任何 Key**。首次使用先 `python scripts/seedream.py --set-key sk-xxx` 写入
  `~/.floorplan-render/config.json`；也可用环境变量 `TOKENHUB_API_KEY` 或单次 `--key` 指定。
  优先级：`--key` > 环境变量 > 本地配置。

## 2. 可用模型

| model | 能力 | 分辨率档 | 参考图上限 |
| --- | --- | --- | --- |
| **`seedream-image-v5.5`** | 参考生图 / 文生图 | 1K / 1.5K / 2K | 10 张 |
| `seedream-image-v5.0-pro` | 同上 | 1K / 1.5K / 2K | 10 张 |
| `seedream-image-v5.0-flash` | 同上 | 1K / 1.5K / 2K | 10 张 |
| `seedream-image-v5.0-lite` | 同上 | 2K / 3K / 4K | 14 张 |

**注意白名单**：Key 的「可访问范围」若只勾了部分模型，调用其他模型会返回
`401006 model ... not in allowed list`。本技能默认只用 `seedream-image-v5.5`；
想换 flash / lite 省钱，先在控制台确认该 Key 已放行。

## 3. 请求参数

| 参数 | 必选 | 类型 | 说明 |
| --- | --- | --- | --- |
| `model` | 是 | string | `seedream-image-v5.5` |
| `prompt` | 是 | string | **≤ 600 字符**。中文建议 ≤ 300 字 |
| `images` | 否 | array[string] | URL 或 **Base64**；≤ 10 张；jpeg/png/webp/bmp/tiff/gif/heic/heif；单张 ≤ 30MB；宽高比 1/16~16 |
| `size` | 否 | string | 两种模式**不可混用**：①档位 `1K`/`1.5K`/`2K`（比例由模型按提示词判断，**本流程默认禁用**）；②显式 `宽x高`，接口总像素 921600~4624220、宽高比 1/16~16。**本项目不传时按 3:4 自动算尺寸，且卡在 260 万像素以内** |
| `output_format` | 否 | string | `png` / `jpeg` |
| `background` | 否 | string | `transparent` / `opaque` / `auto`（v5.5 支持 auto） |
| `response_format` | 否 | string | `url`（12 小时有效）/ `b64_json` |
| `watermark` | 否 | boolean | `true` 会在右下角加"AI 生成"。**本流程一律 `false`** |
| `optimize_prompt_options.mode` | 否 | string | `standard`（默认）/ `fast`（v5.5 **不支持** fast） |
| `sequential_image_generation` | 否 | string | `auto` 可能出组图；`disabled` 只出一张。**官方标注仅 v5.0-lite 支持**，v5.5 传了不一定生效 |
| `layer_decomposition` | 否 | boolean | 图层拆分，本流程不用 |

**没有** 负面提示词、seed、steps、CFG、参考图权重这类参数。所有约束必须写成**正向描述**
（例：不写 "no text"，写 "纯白背景、画面无任何文字与标注"）。

## 4. 返回

```json
{
  "created": 1787309367,
  "data": [
    { "output_format": "jpeg", "size": "2048x2048", "url": "https://xxx.jpg" }
  ],
  "model": "doubao-seedream-5-0-pro-260628",
  "tokenhub_usage": { "total_tokens": 60000 },
  "request_id": "f781d9dc-..."
}
```

- `data` 是**数组**，可能多张（组图）。脚本已按 `-1/-2` 后缀分别落盘。
- `url` 是临时地址，**12 小时过期**。脚本收到即下载，不要只存链接。
- `request_id` 报错时一定要带上，方便提工单。

## 5. 尺寸怎么定（本流程硬规矩）

**两条默认值（2026-11-19 定）：①不指定就是 3:4；②总像素不超过 260 万。**
脚本里 `DEFAULT_RATIO = 3/4`、`MAX_PIXELS_USER = 2600000`，不传 `--size` 自动算出 **1392x1856（258 万）**。
超过 260 万会直接报错并给出同比例改小的尺寸；要突破必须 `--allow-big`。

同一套图**必须用显式 WxH**，不要用 `1K/1.5K/2K` 档位——档位让模型自己猜比例，
会导致十张图比例不齐，成套感立刻崩。`2K` 档位在脚本里已被拦下。

常用组合（均为 16 的倍数，且不超 260 万）：

| 用途 | size | 像素 |
| --- | --- | --- |
| **默认（所有空间）** | `3:4` → `1392x1856` | 2 583 552 |
| 明确要横构图 | `16:9` → `2144x1200` | 2 572 800 |
| 方厅 / 主卧套间 | `1:1` → `1600x1600` | 2 560 000 |
| 旧版横图（仍在限内，可兼容） | `2048x1152` | 2 359 296 |
| 鸟瞰（跟随户型比例） | `--size auto` | ≤2 359 296 |

`--size auto` 会读第一张参考图的宽高比，按 2.36MP 面积算出 WxH，
所以鸟瞰图的画布比例天然等于户型图比例——这是保证"鸟瞰不变形"的关键，也是**唯一不套 3:4 的地方**。

## 6. 参考图怎么传

- 本地文件必须走 **Base64**（`images` 数组）。`url` 方式要求公网可访问，本机文件做不到。
- 编码形态有两种写法，文档没写死用哪种，脚本默认 **自动兜底**（`--img-enc auto`）：
  1. `data:image/jpeg;base64,xxxx`（Data URL 前缀）—— 先试这个，火山系接口的通行写法；
  2. 纯 `xxxx`（裸 base64）—— 第一种被判 `InvalidParameter` 时自动退到这种。
  想手动指定就 `--img-enc dataurl` / `--img-enc raw`。
- **POST body 总大小 ≤ 20MB**。脚本默认把每张压到长边 2048 / JPEG q88（约 300~600KB），
  10 张也就 5MB 左右，安全。放宽到 `--max-side 4096` 前先算一下。
- 带 alpha 的 PNG 会被压到白底——户型图多是黑线白底，没问题；深色底图会被"洗白"。

## 7. 实测踩到的错误码

| HTTP | code | 含义 | 处理 |
| --- | --- | --- | --- |
| 402 | `401007` | 无免费额度且未开启后付费 | 去 https://console.cloud.tencent.com/tokenhub/inference 开通后付费 |
| 400 | `401006` | 模型不在白名单 | 换回 `seedream-image-v5.5` |
| 400 | `InvalidParameter` | 参数非法（size 越界 / 图太多 / 格式不支持） | 按第 3、5 节自查 |
| 400 | `MissingParameter` | 缺 `%s`（多为缺 `prompt`） | 补参数 |

> **401007 属于账号配置问题，不是代码问题。** 出现时去模型广场给该模型开启按量付费即可；
> 开通前所有调用立即返回 402，不扣费也不耗时，可以放心用 `--print-curl` / `--validate-only` 调试。

## 8. 计费

响应体里的 `tokenhub_usage.total_tokens` 就是本次消耗。
实测量级（2026-11）：**2048×1152 单图约 1.8 万 tokens，单张耗时 20 秒左右**；一套 8 间约 15 万 tokens。
**单价不要凭记忆报给用户**，要报就让他去控制台看账单。

## 9. 能力边界（必须向用户讲清）

这个接口是**参考生图**，不是结构控制型（没有 ControlNet / 线稿约束 / 深度图）。
意味着：

- 它按自己的理解重建画面，**墙体走向、房间数量只能"尽量"贴近**平面图，不能像素级复刻。
- 提高贴合度的有效手段，按性价比排序：
  1. 提示词里**逐条写出房间名 + 相对方位 + 数量**（比"还原平面图"这种笼统话有效得多）；
  2. 用 `--size auto` 锁画布比例；
  3. 出 2 版让用户挑，而不是指望一版准；
  4. 分区实景阶段以**鸟瞰图**为参考图（比平面图更能约束内部布局）。
- 反过来，**不要**承诺"和户型图一模一样"，也不要因为一次不准就判定接口坏了。
