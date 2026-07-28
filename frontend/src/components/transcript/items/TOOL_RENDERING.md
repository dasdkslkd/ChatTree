# 工具调用渲染系统

本目录下的 `ToolCallRenderer.tsx` 负责所有工具调用的可视化渲染，替代了过去直接输出原始 JSON 的方式。

## 架构概览

```
AssistantProcessItem.tsx
  └─ summarizeToolCall()  ← 计算折叠态摘要
  └─ AssistantProcessTimeline.tsx
       └─ ToolCallCard  ← 统一可折叠容器
ToolApprovalCard.tsx
  └─ ToolCallPreview     ← 工具审批时的结构化预览
```

核心位置：[ToolCallRenderer.tsx](file:///d:/Workspace/ChatTree/frontend/src/components/transcript/items/ToolCallRenderer.tsx)

## 渲染规则

### 统一容器（ToolCallCard）

- **折叠态**：工具图标 + 工具名 + 摘要 + 状态指示 + 展开箭头
- **展开态**：结构化详情 + 复制按钮
- 展开/折叠使用 CSS `grid-template-rows` 过渡动画，支持任意高度内容
- 长结果列表（文件列表、匹配列表、搜索结果）有 `max-height: 260px` 滚动容器
- 窄屏（≤640px）自动收紧摘要宽度和列表高度

### 工具类型注册表

所有工具在 `TOOL_SPECS` 中注册，未命中的工具自动走 `defaultSpec` 兜底。每个 spec 包含：

| 字段       | 类型                                           | 说明                                  |
| ---------- | ---------------------------------------------- | ------------------------------------- |
| `icon`     | `LucideIcon`                                   | 折叠态显示的图标                       |
| `summary` | `(args, result, status) => string`             | 折叠态摘要，限制在 80 字符内           |
| `detail`   | `(args, result, status) => ReactNode`          | 展开态结构化内容                       |

### 已注册工具

| 工具名                | 折叠态摘要                          | 展开态详情                                      |
| --------------------- | ----------------------------------- | ----------------------------------------------- |
| `shell`               | 命令字符串（截断 80 字符）          | 命令块 + 退出码/cwd/超时 + stdout + stderr       |
| `grep`                | `pattern @ path · N 处匹配/个文件` | 正则/cwd/模式/匹配数 + 文件列表或匹配列表        |
| `glob`                | `patterns @ path · N 个文件`       | 模式/cwd/排序/文件数 + 文件路径列表             |
| `read` / `read_file`  | `path L起-止`                       | 文件路径 + 行范围 + 文件内容                    |
| `edit` / `patch` / `apply_patch` | `path`                  | 文件名 + 应用状态 + 摘要/hunks/内容             |
| `write` / `write_file`| `path`                              | 同 edit                                         |
| `fetch_url`           | URL（截断 80 字符）                 | URL + 状态码 + 标题 + 内容                       |
| `web_search`          | `query · N 项`                     | 查询词 + 结果列表（标题/URL/摘要）               |
| 其他（默认兜底）       | 错误信息 或 `command/pattern/path` 或 "执行中..." | 参数 JSON + 结果 JSON，含复制按钮       |

### 错误处理

所有工具的 `detail` 都会先调用 `getErrorMessage(result)`，当结果包含 `{ error: { type, message } }` 时，用红色边框的 `tc-pre-error` 块显示错误信息，跳过正常渲染。

## 新增工具类型集成指南

### 1. 在 `TOOL_SPECS` 注册

打开 [ToolCallRenderer.tsx](file:///d:/Workspace/ChatTree/frontend/src/components/transcript/items/ToolCallRenderer.tsx)，在 `TOOL_SPECS` 对象中添加：

```typescript
const TOOL_SPECS: Record<string, ToolSpec> = {
  // ...existing tools
  my_tool: myToolSpec(),
};

function myToolSpec(): ToolSpec {
  return {
    icon: MyIcon,  // 从 lucide-react 导入
    summary: (args, result, status) => {
      // 从 args/result 提取最关键的 1 行信息
      // 长度建议 ≤ 80 字符，超出用 truncate(text, 80)
      return asString(args.my_key);
    },
    detail: (args, result, status) => {
      // 渲染结构化内容，复用 PreBlock / MetaRow / CopyButton / EmptyState
      return (
        <>
          <MetaRow items={[...]} />
          {status === 'running' && !result && <EmptyState text="执行中..." />}
          {result && <PreBlock variant="output">{asString(result.content)}</PreBlock>}
        </>
      );
    },
  };
}
```

### 2. 复用现有原子组件

| 组件         | 用途                                | 关键 props                                  |
| ------------ | ----------------------------------- | ------------------------------------------- |
| `PreBlock`   | 代码/输出/错误块，自带滚动和等宽字体 | `variant: 'cmd' \| 'output' \| 'error'`     |
| `MetaRow`    | 键值对元信息行（退出码/cwd 等）      | `items: [{ label, value, tone? }]`           |
| `CopyButton` | 复制按钮，点击后 2 秒回显"已复制"   | `text, label, variant?: 'default' \| 'subtle'` |
| `EmptyState` | 空状态/运行中文案                   | `text`                                       |

### 3. 数据访问助手

| 函数             | 签名                              | 说明                                  |
| ---------------- | --------------------------------- | ------------------------------------- |
| `asObject`       | `(value: unknown) => ToolResult`  | 安全转对象，非对象返回 null           |
| `asString`       | `(value: unknown) => string`      | 安全转字符串，null/undefined 返回 ''   |
| `asNumber`       | `(value: unknown) => number \| null` | 安全转数字，非有限数返回 null         |
| `asArray`        | `(value: unknown) => unknown[]`   | 安全转数组，非数组返回 []             |
| `truncate`       | `(text, max) => string`           | 超长截断并以 `…` 结尾                 |
| `singleLine`     | `(text) => string`                | 压缩空白为单空格                       |
| `getErrorMessage`| `(result) => string \| null`       | 提取 `{error: {message}}` 错误信息     |

### 4. CSS 类约定

所有工具渲染相关类名以 `tc-` 前缀定义在 [App.css](file:///d:/Workspace/ChatTree/frontend/src/App.css) 的 `Tool calls` 区段。新增工具复用现有类即可，避免新增样式。

### 5. 测试

在 [toolCallRendering.test.cjs](file:///d:/Workspace/ChatTree/frontend/test/toolCallRendering.test.cjs) 中为新工具添加：

- `testSummarizeFor<MyTool>`：验证折叠态摘要字符串
- `testRegistryContainsAllExpectedTools`：将工具名加入 `expectedTools` 数组

## 测试与验证

运行测试：

```bash
cd frontend
node test/toolCallRendering.test.cjs
```

覆盖范围：
- 9 种工具的摘要提取逻辑（实际执行转译后的 `summarizeToolCall`）
- 长命令截断、错误结果处理、无效 JSON 容错
- 组件结构、复制按钮、CSS 类定义、响应式断点、滚动容器
- 现有 `assistantProcessItem` 和 `toolApprovalRendering` 测试无回归
