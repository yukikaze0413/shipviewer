# 损伤树相机位置 JSON 填写说明

模板文件：`damage-camera-poses.template.json`

建议正式使用时复制为：

```text
assets/json/damage-camera-poses.json
```

## 字段说明

每个 `poses` 项对应一个损伤树节点。


| 字段 | 说明 |
| --- | --- |
| `node_id` | 损伤树节点 ID，必须和 `damage-tree-nodes.csv` 第一列一致。 |
| `name` | 仅用于人工识别，程序主要按 `node_id` 匹配。 |
| `position` | 调试功能里的 `position`，三维数组 `[x, y, z]`。 |
| `focal_point` | 调试功能里的 `focal_point`，三维数组 `[x, y, z]`。 |
| `direction` | 调试功能里的 `direction (center→camera)`，三维数组 `[x, y, z]`。 |
| `distance` | 调试功能里的 `distance`，数字。 |

## 填写方法

1. 打开程序里的相机调试功能。
2. 手动旋转、缩放到希望点击损伤树后显示的角度。
3. 把调试框里的 `position`、`focal_point`、`direction`、`distance` 复制到对应 `node_id`。
4. 保存 JSON。
5. 后续实现读取逻辑后，重启程序即可生效。

## 示例

```json
{
  "node_id": "WINDOW_SIDE",
  "name": "侧窗",
  "position": [12.34, 8.9, 56.78],
  "focal_point": [1.2, 3.4, 5.6],
  "direction": [0.12, 0.34, 0.93],
  "distance": 62.5
}
```

## 注意事项

- JSON 文件不能写注释。
- 所有字段名必须使用英文双引号。
- 数组里必须是数字，不要加单位。
- 最后一个对象后面不要加逗号。
- `position` 和 `focal_point` 一般已经能确定相机位置；保留 `direction` 和 `distance` 是为了和调试框数据一一对应，方便后续校验。
