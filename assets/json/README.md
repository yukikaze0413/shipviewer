# 设备详情 JSON 填写指南

设备详情维护在 `device-catalog.json` 中。程序会根据点击的模型对象或左侧损伤树节点，查找对应设备并在右侧直接展示设备名称、图片、材质、厚度、尺寸和功能。

## 文件格式

`device-catalog.json` 是一个 JSON 数组，每个 `{}` 表示一个设备或一个层级说明。

```json
[
  {
    "model_name": "Object_47",
    "display_name": "车窗",
    "damage_leaf_id": "WINDOW_SIDE",
    "image_path": "images/devices/object-47.png",
    "material": "钢化玻璃",
    "thickness": "6 mm",
    "size": "1200 x 600 mm",
    "function": "侧向观察与防护"
  }
]
```

## 字段说明

| 字段 | 是否必填 | 填写说明 |
| --- | --- | --- |
| `model_name` | 是 | 模型对象名称，必须和模型导入后显示的对象名一致，例如 `Object_47` 或 `对象_381`。 |
| `display_name` | 是 | 右侧展示的设备名称，例如 `车窗`。 |
| `damage_leaf_id` | 建议填写 | 损伤树节点 ID，用于点击损伤树时反查并高亮对应模型，例如 `WINDOW_SIDE`。 |
| `image_path` | 建议填写 | 设备图片路径，推荐放在 `assets/images/devices/` 下，并填写相对 `assets/` 的路径。 |
| `material` | 是 | 材质，例如 `钢化玻璃`、`橡胶`、`铝合金`。 |
| `thickness` | 是 | 厚度，例如 `6 mm`。无固定厚度时可填 `不适用`。 |
| `size` | 是 | 尺寸，例如 `1200 x 600 mm`。尺寸未知时先填 `待补充`。 |
| `function` | 是 | 功能说明，建议一句话说明该设备用途。 |

## 图片路径规则

推荐把图片放在：

```text
assets/images/devices/
```

然后在 JSON 中这样写：

```json
"image_path": "images/devices/object-47.png"
```

支持 `.png`、`.jpg`、`.jpeg` 等常见图片格式。图片不存在时，程序会显示“设备图片不存在”，不会崩溃。

## 匹配关系

程序用 `model_name` 匹配模型对象：

```text
模型对象名称  <->  device-catalog.json 的 model_name
```

点击损伤树节点时，用 `damage_leaf_id` 找到应高亮的设备行：

```text
damage-tree-nodes.csv 的 节点ID  <->  device-catalog.json 的 damage_leaf_id
```

因此新增设备时，最重要的是保证 `model_name` 和模型对象名一致，并把 `damage_leaf_id` 填成对应的损伤树节点 ID。

## 新增设备步骤

1. 在 `assets/images/devices/` 放入设备图片，例如 `object-200.png`。
2. 打开 `assets/json/device-catalog.json`。
3. 复制一个已有设备对象，粘贴到数组末尾。
4. 修改 `model_name`、`display_name`、`damage_leaf_id` 和各项参数。
5. 注意对象之间用英文逗号 `,` 分隔，最后一个对象后面不要加逗号。
6. 保存文件后重启程序。

## 常见错误

- JSON 里必须使用英文双引号 `"`，不能用中文引号。
- 每个字段之间用英文逗号 `,` 分隔。
- 最后一个设备对象后面不要加逗号。
- `model_name` 填错会导致点击模型后找不到设备详情。
- `image_path` 不要写成 `assets/images/devices/object-47.png`，推荐写 `images/devices/object-47.png`。

## 可复制模板

```json
{
  "model_name": "Object_编号",
  "display_name": "设备名称",
  "damage_leaf_id": "损伤节点ID",
  "image_path": "images/devices/object-编号.png",
  "material": "待补充",
  "thickness": "待补充",
  "size": "待补充",
  "function": "待补充"
}
```
