# PDF 目录说明

将部件说明 PDF 放在当前目录下。

在 [`public/assets/part-pdf-catalog.csv`](../part-pdf-catalog.csv) 中维护三列：

- `模型名称`
- `中文名称`
- `PDF路径`

其中 `PDF路径` 推荐填写相对于 [`public/assets/pdfs/`](./) 的文件名，例如：

```csv
模型名称,中文名称,PDF路径
pump_01,燃油泵,pump_01.pdf
```

也可以直接填写以 `/assets/pdfs/` 开头的绝对路径，例如：

```csv
模型名称,中文名称,PDF路径
pump_01,燃油泵,/assets/pdfs/pump_01.pdf
```
