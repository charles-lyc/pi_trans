# PiTrans 配置与运维

[README](../README.md) · [中文 README](../README.zh-CN.md) · [设计说明](DESIGN.md)

---

## 1. 访问

```
http://192.168.50.47
```

- IP 由路由器 DHCP 分配，2026-11-10 从 `.44` 变成了 `.47`。连不上时用 `arp -a` 找 MAC 前缀为 `dc-a6-32` 的设备；建议在路由器上绑定固定 IP。
- 换到其他网络（会场 Wi-Fi、手机热点）后 IP 会变，可以在路由器或热点的已连接设备列表里找设备名 `chao`。

---

## 2. 目录结构

```
pi_trans/                      （本仓库）
├── README.md                  项目简介（英文）
├── README.zh-CN.md            项目简介（中文）
├── docs/
│   ├── DESIGN.md              设计说明：技术方案、模型选择、后续优化
│   └── OPERATIONS.md          配置与运维（本文档）
└── pitrans/                   ＝ 树莓派上的 ~/pitrans
    ├── app.py                 FastAPI 服务：WebSocket、翻译调度、统计
    ├── asr.py                 录音 + VAD + 语音识别（SenseVoice / Zipformer / Qwen 同传）
    ├── translators.py         翻译引擎（Google/Bing/MyMemory/LLM/Qwen-MT/Qwen 同传配置）
    ├── config.example.json    配置模板（不含 API Key 和 Wi-Fi 密码），部署时复制为 config.json
    ├── requirements.txt       Python 依赖
    ├── bench.py               识别测试：两个识别模型识别同一段录音并对比
    ├── llmtest.py             翻译测试：指定引擎和 Google 对比译文和耗时
    ├── static/index.html      网页
    └── system/                树莓派系统配置副本
        ├── pitrans.service        → /etc/systemd/system/pitrans.service
        ├── pitrans-wifi.service   → /etc/systemd/system/pitrans-wifi.service（开机同步 Wi-Fi 列表）
        ├── wifi_sync.py           把 config.json 的 `wifi` 写入 NetworkManager
        ├── 95proxy                → /etc/apt/apt.conf.d/95proxy
        └── environment.proxy      → 追加到 /etc/environment
```

不进仓库的内容：

- `pitrans/config.json`：真正的配置，含 API Key 和 Wi-Fi 密码，已在 `.gitignore` 中排除。程序运行时读的是树莓派上的 `~/pitrans/config.json`；本机在仓库外另存一份备份。
- 模型压缩包、麦克风资料、百炼 API Key 文件：留在本机仓库外的目录中。
- 树莓派上的 `~/pitrans/.venv/`（Python 虚拟环境，约 175 MB，可用 requirements.txt 重建）、`~/pitrans/models/`（模型文件，约 760 MB）、`~/pitrans/me.wav`（测试录音）、`~/pitrans/backup-20261110/`（接入 Qwen 同传之前的备份）。

> 更新树莓派上的代码时，**不要用 `config.example.json` 覆盖树莓派上的 `config.json`**，否则 API Key 和 Wi-Fi 会被清空。

---

## 3. 配置说明（`config.json`）

| 键                          | 当前值（树莓派）                    | 说明                                                            |
| -------------------------- | --------------------------- | ------------------------------------------------------------- |
| `direction`                | `zh2en`                     | 翻译方向：`zh2en` / `en2zh` / `auto`（网页可改）                         |
| `engine`                   | `qwen-live`                 | 首选引擎（网页可改）；`qwen-live` 表示使用云端同传                               |
| `fallback`                 | `true`                      | 首选引擎失败时，按 `engines` 中的顺序依次尝试其他引擎                              |
| `proxy`                    | `http://192.168.50.86:7890` | 代理地址（HTTP/SOCKS5），给 `use_proxy: true` 的引擎使用                   |
| `wifi`                     | 2 个网络                       | 预存的 Wi-Fi 列表，开机时写入系统（见第 6 节）                                  |
| `audio_device`             | `auto`                      | `auto` 表示使用第一个录音设备；也可以写成具体设备，如 `plughw:CARD=Microphone,DEV=0` |
| `input_gain`               | `4.0`                       | 软件增益。当前的 Timesintelli USB 麦克风硬件音量已经是最大，仍然偏小，所以设为 4；过大会削顶失真    |
| `asr`                      | `sensevoice`                | 本地识别：`sensevoice` 或 `zipformer`（使用 Qwen 同传时不生效）               |
| `asr_language`             | `zh`                        | SenseVoice 的识别语言（`zh` / `en` / `auto` 等）                      |
| `vad.threshold`            | `0.25`                      | 判断"有人说话"的门槛，越低越灵敏；环境吵时可调到 0.5～0.7（只影响本地识别）                    |
| `vad.min_silence_duration` | `0.7`                       | 停顿多少秒算一句结束。越大句子越完整，越小出字越快                                     |
| `vad.min_speech_duration`  | `0.5`                       | 短于这个时长的声音忽略（过滤咳嗽、语气词）                                         |
| `vad.max_speech_duration`  | `10`                        | 一直不停顿时，最长多少秒强制切一句                                             |
| `num_threads`              | `4`                         | 识别使用的 CPU 线程数                                                 |
| `context_sentences`        | `3`                         | 给大模型带上前几句的上下文                                                 |
| `glossary`                 | 211 条                       | 术语表，见第 4 节                                                    |
| `engines.*`                |                             | 各翻译引擎，**书写顺序就是失败时的切换顺序**                                      |

引擎字段：

- 共有：`type`（`google`/`bing`/`mymemory`/`llm`/`qwen-mt`/`qwen-live`，缺省时等于引擎名）、`label`（网页显示名）、`use_proxy`、`timeout`
- `llm`：`base_url`、`api_key`、`model`、`prompt`（追加到系统提示词）、`extra_body`（附加请求参数，例如 `{"thinking": {"type": "disabled"}}`）
- `qwen-mt`：`base_url`、`api_key`、`model`、`domains`（领域或风格提示）
- `qwen-live`：见下文。它不参与失败切换

**修改配置的注意事项**：配置只在启动时读取，改完要重启服务。而且在网页上切换方向、引擎或暂停时，程序会把内存中的整份配置写回文件，所以手动编辑前先停服务：

```bash
sudo systemctl stop pitrans
nano ~/pitrans/config.json
sudo systemctl start pitrans
```

### 3.1 Qwen 同传（`engines.qwen-live`）

```json
"qwen-live": {
  "type": "qwen-live",
  "label": "Qwen 同传",
  "use_proxy": false,
  "url": "wss://<workspaceId>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
  "api_key": "",
  "model": "qwen3.8-livetranslate-flash-realtime",
  "silence_ms": 600,
  "vad_threshold": 0.5
}
```

- **百炼的 Key 绑定业务空间**：`url` 要用 Key 文件里的 `apiHost`。通用地址是北京区 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime`、新加坡区 `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`（业务空间地址把 `cn-beijing` 换成 `ap-southeast-1`）。**Key 要和区域对应**。
- `silence_ms`：停顿多少毫秒算一句结束（默认 600，越小越快，太小会把一句话切碎）；设为 `0` 则使用模型默认的 `speaker_detection`（不推荐，会把长段落当成一句）。
- `vad_threshold`：服务端判断有人说话的门槛，默认 0.5。
- 树莓派直连即可，不需要走代理。
- 开通服务、查价格：[阿里百炼模型页](https://bailian.console.aliyun.com/cn-beijing/model/market/detail/qwen3.8-livetranslate-flash-realtime?serviceSite=asia-pacific-china&ref=search_result&kw=live)

---

## 4. 术语表（`glossary`）

当前共 211 条，面向智能硬件 / KiCad / PCB（74 条）、AI（52 条）以及嵌入式 / 硬件 / 网络补充（85 条）场景（2026-11-11 加入）。格式是 `{"zh": "原理图", "en": "schematic"}`，中→英、英→中两个方向通用。

| 引擎                       | 用法                                      | 生效时机                                  |
| ------------------------ | --------------------------------------- | ------------------------------------- |
| Qwen 同传                  | 作为 `translation.corpus.phrases` 热词      | 建立会话时（启动、切换方向、重连）。已验证两个方向都能接受全部 211 条 |
| GLM / DeepSeek           | 写进系统提示词的"Always use these translations" | 每句                                    |
| Qwen-MT                  | 作为 `translation_options.terms`          | 每句                                    |
| Google / MyMemory / Bing | 不支持，不使用                                 | —                                     |

| 类别         | 术语                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| KiCad / 会议 | KiCad、KiCon Asia、开源硬件 = open-source hardware、智能硬件 = smart hardware                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 设计流程       | 原理图 = schematic、原理图符号 = schematic symbol、符号库 = symbol library、封装 = footprint、封装库 = footprint library、元器件 = component、网表 = netlist、布局 = layout、布线 = routing、走线 = trace、差分对 = differential pair、等长 = length matching、阻抗匹配 = impedance matching                                                                                                                                                                                                                                                                                            |
| PCB        | 印刷电路板 = PCB、过孔 = via、焊盘 = pad、铺铜 = copper pour、丝印 = silkscreen、阻焊 = solder mask、层叠 = stackup、四层板 = four-layer board、飞线 = ratsnest、地平面 = ground plane、光绘文件 = Gerber files、钻孔文件 = drill files                                                                                                                                                                                                                                                                                                                                             |
| 检查 / 输出    | 设计规则检查 = design rule check、电气规则检查 = electrical rules check、物料清单 = bill of materials                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 元件         | 电阻 = resistor、电容 = capacitor、电感 = inductor、二极管 = diode、晶体管 = transistor、场效应管 = MOSFET、晶振 = crystal oscillator、低压差稳压器 = LDO regulator、去耦电容 = decoupling capacitor                                                                                                                                                                                                                                                                                                                                                                        |
| 嵌入式 / 物联网  | 嵌入式 = embedded、物联网 = IoT、单片机 = microcontroller、芯片 = chip、传感器 = sensor、执行器 = actuator、固件 = firmware、烧录 = flashing、开发板 = development board、串口 = UART、低功耗蓝牙 = Bluetooth Low Energy、无线模组 = wireless module、天线 = antenna                                                                                                                                                                                                                                                                                                                     |
| 嵌入式补充      | 实时操作系统 = RTOS、引导程序 = bootloader、中断 = interrupt、中断服务程序 = interrupt service routine、看门狗 = watchdog timer、定时器 = timer、通用输入输出 = GPIO、模数转换器 = ADC、数模转换器 = DAC、脉宽调制 = PWM、直接存储器访问 = DMA、寄存器 = register、外设 = peripheral、闪存 = flash memory、驱动程序 = device driver、交叉编译 = cross-compilation、调试器 = debugger、裸机 = bare metal、内核 = kernel、设备树 = device tree、片上系统 = SoC、微处理器 = microprocessor、现场可编程门阵列 = FPGA、数字信号处理 = digital signal processing、串行外设接口 = SPI、集成电路总线 = I2C、控制器局域网 = CAN bus、低功耗模式 = low-power mode、睡眠模式 = sleep mode、空中升级 = OTA update |
| 硬件补充       | 电源 = power supply、锂电池 = lithium battery、充电管理 = charge management、降压转换器 = buck converter、升压转换器 = boost converter、稳压器 = voltage regulator、运算放大器 = op-amp、比较器 = comparator、继电器 = relay、光耦 = optocoupler、磁珠 = ferrite bead、保险丝 = fuse、连接器 = connector、排针 = pin header、散热片 = heatsink、外壳 = enclosure、电压 = voltage、接地 = grounding、上拉电阻 = pull-up resistor、下拉电阻 = pull-down resistor、电机 = motor、步进电机 = stepper motor、舵机 = servo、编码器 = encoder、液晶屏 = LCD、发光二极管 = LED、摄像头模组 = camera module、麦克风阵列 = microphone array、3D打印 = 3D printing          |
| 网络         | 以太网 = Ethernet、路由器 = router、交换机 = network switch、网关 = gateway、协议栈 = protocol stack、网络协议 = network protocol、IP地址 = IP address、子网掩码 = subnet mask、域名解析 = DNS resolution、带宽 = bandwidth、网络延迟 = network latency、吞吐量 = throughput、丢包 = packet loss、数据包 = packet、防火墙 = firewall、虚拟专用网络 = VPN、网状网络 = mesh network、低功耗广域网 = LPWAN、窄带物联网 = NB-IoT、套接字 = socket、端口 = port、光纤 = optical fiber、网线 = Ethernet cable、服务器 = server、客户端 = client、云平台 = cloud platform                                                                                   |
| 生产 / 测试    | 原型 = prototype、打样 = prototyping、量产 = mass production、贴片 = SMT assembly、回流焊 = reflow soldering、焊接 = soldering、示波器 = oscilloscope、万用表 = multimeter、逻辑分析仪 = logic analyzer、仿真 = simulation                                                                                                                                                                                                                                                                                                                                                 |
| 质量 / 功耗    | 信号完整性 = signal integrity、电源完整性 = power integrity、电磁兼容 = EMC、静电防护 = ESD protection、功耗 = power consumption                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 品牌         | 树莓派 = Raspberry Pi、嘉立创 = JLCPCB、立创商城 = LCSC、乐鑫 = Espressif                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| AI 基础      | 人工智能 = AI、通用人工智能 = AGI、生成式人工智能 = generative AI、机器学习 = machine learning、深度学习 = deep learning、神经网络 = neural network、大语言模型 = large language model、大模型 = LLM、多模态 = multimodal、智能体 = AI agent、具身智能 = embodied AI                                                                                                                                                                                                                                                                                                                             |
| AI 使用      | 提示词 = prompt、提示词工程 = prompt engineering、上下文窗口 = context window、Token = token、思维链 = chain of thought、函数调用 = function calling、模型上下文协议 = Model Context Protocol、幻觉 = hallucination、检索增强生成 = retrieval-augmented generation、向量数据库 = vector database、嵌入模型 = embedding model                                                                                                                                                                                                                                                                  |
| AI 训练 / 模型 | 预训练 = pre-training、模型训练 = model training、微调 = fine-tuning、强化学习 = reinforcement learning、知识蒸馏 = knowledge distillation、模型推理 = model inference、推理模型 = reasoning model、注意力机制 = attention mechanism、扩散模型 = diffusion model、混合专家 = mixture of experts、参数量 = parameter count、开源模型 = open-source model、数据集 = dataset、数据标注 = data annotation、模型量化 = model quantization                                                                                                                                                                          |
| AI 硬件 / 应用 | 算力 = computing power、神经网络处理器 = NPU、边缘计算 = edge computing、端侧AI = on-device AI、微型机器学习 = TinyML、计算机视觉 = computer vision、自然语言处理 = natural language processing、语音识别 = speech recognition、语音合成 = speech synthesis、目标检测 = object detection、图像识别 = image recognition                                                                                                                                                                                                                                                                            |
| AI 公司 / 模型 | 通义千问 = Qwen、深度求索 = DeepSeek、智谱 = Zhipu AI、英伟达 = NVIDIA                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 其他         | 加泰罗尼亚 = Catalonia（测试用）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

取舍：

- **不加日常用语里很常见的词**，如"网络"（net）、"符号"（symbol）、"层"（layer）、"延迟"、"交换"。强制翻译会把"网络连接"这类普通说法译错，所以改用"网络协议""网络延迟""交换机 = network switch"这类说法。"电流"（current）也没加，英→中时会误伤"当前"的意思。
- **"推理""训练""量化""嵌入"单独都有日常或其他领域的意思**（如"量化交易"、"嵌入式"），所以只加了"模型推理""推理模型""模型训练""模型量化""嵌入模型"这类带限定的说法。
- 有几条英文本身也有日常含义：port、server、client、motor、relay、interrupt、register、kernel。偏硬件的演讲里一般没问题；如果演讲中常用它们的日常含义，可以删掉。
- **"封装"固定译为 footprint**，这是 KiCad 的用法。说"芯片封装"（package）时可能会被误译，必要时可以再加一条"芯片封装 = chip package"。
- **每条的英文不能重复**。英→中方向会把英文当作键，重复的英文会互相覆盖。
- **识别错误（如 "KiCad" 被听成 "Key CAD"）不能用术语表修正**：术语表是双向的，加进去后英→中方向反而会把正确的 KiCad 换成错的。这类错误需要在出字幕前另加一步文本替换（见[设计说明](DESIGN.md)的后续优化方向）。
- 术语只是给模型的提示，不保证每次都按术语翻译。kicon-live-translate 自带的 7 条 KiCad 术语已经全部包含在本表中。

修改方法：按第 3 节的注意事项编辑树莓派上的 `glossary`，然后重启服务。改完后同步到仓库的 `config.example.json`，两边保持一致。

---

## 5. 日常运维

```bash
ssh chao@192.168.50.47

sudo systemctl status pitrans        # 状态
sudo systemctl restart pitrans       # 重启（改配置后需要）
journalctl -u pitrans -f             # 实时日志（识别、翻译耗时、原文 => 译文）
journalctl -u pitrans -n 50 | grep vad   # 确认 VAD 参数已生效
arecord -l                           # 查看麦克风
```

- **换麦克风**：直接拔插即可，服务会在 2 秒内自动重连，不需要重启。
- **测试翻译引擎**：`cd ~/pitrans && .venv/bin/python llmtest.py glm`（可换成 `deepseek` 或 `qwen`），会和 Google 对比译文和耗时。
- **测试识别**：`.venv/bin/python bench.py me.wav`，用两个识别模型分别识别同一段录音并对比。测试前先 `sudo systemctl stop pitrans`，避免两边抢 CPU。

---

## 6. 预存 Wi-Fi

树莓派用 NetworkManager 管理网络。它记住多个网络后，会自动连上范围内的任意一个，所以换场地前把账号密码提前填好就行。

1. 编辑 `~/pitrans/config.json` 里的 `wifi`（先停服务，见第 3 节），每个网络一项：
   
   ```json
   "wifi": [
     {"ssid": "会场WiFi", "password": "<密码>", "priority": 10, "hidden": false},
     {"ssid": "手机热点", "password": "<密码>", "priority": 5, "hidden": false}
   ]
   ```
   
   - `ssid` 为空的条目会被跳过；`password` 为空表示开放网络
   - `priority`：同时能连上多个网络时，数字大的优先
   - `hidden`：隐藏网络（不广播名字）填 `true`

2. 写入系统：`sudo systemctl start pitrans-wifi`（或者直接重启树莓派，开机时会自动执行）

3. 确认：`nmcli connection show` 能看到 `pitrans-<ssid>` 的条目

说明：

- 不在 `wifi` 列表里、早先直接配在系统里的网络，可以单独设置优先级：`sudo nmcli connection modify <连接名> connection.autoconnect-priority 5`。

- 优先级只在开机或断网后挑选网络时起作用，已经连着的网络不会因为出现了更高优先级的网络而自动切换。
- 改了已有条目的密码或优先级，再执行一次第 2 步就会更新；从列表里删掉的网络**不会**自动从系统中删除，需要手动删：`sudo nmcli connection delete pitrans-<ssid>`。
- 手机热点如果只开了 WPA3，可能连不上，改成 WPA2 或 WPA2/WPA3 混合模式。iPhone 热点要打开"允许其他人加入"，最好再打开"最大兼容性"。

---

## 7. 从零部署

1. **系统**：刷 Raspberry Pi OS Lite，开启 SSH，用户名 `chao`。

2. **代理**（可选）：把 `system/environment.proxy` 的内容追加到 `/etc/environment`，把 `system/95proxy` 复制到 `/etc/apt/apt.conf.d/`。

3. **代码**：把仓库的 `pitrans/` 复制到 `~/pitrans`，然后 `cp config.example.json config.json`。

4. **依赖**：
   
   ```bash
   cd ~/pitrans
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

5. **模型**：在 `~/pitrans/models/` 下解压以下文件（GitHub 慢的话可以先在电脑上下载，再上传）：
   
   - https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
   - https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
   - （可选）https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2

6. **API Key 和 Wi-Fi**：在 `config.json` 中填写需要用的引擎的 `api_key`，以及 `wifi` 列表：
   
   - 阿里百炼：https://bailian.console.aliyun.com（Qwen-MT 和 Qwen 同传共用同一个 Key；Qwen 同传要在模型广场开通 `qwen3.8-livetranslate-flash-realtime`）
   - DeepSeek：https://platform.deepseek.com
   - 智谱：https://open.bigmodel.cn

7. **开机自启**：
   
   ```bash
   sudo cp system/pitrans.service system/pitrans-wifi.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now pitrans pitrans-wifi
   ```
   
   服务监听 80 端口（通过 `CAP_NET_BIND_SERVICE` 授权，不需要 root）。`pitrans-wifi` 会在开机时把 `wifi` 列表写入系统。
