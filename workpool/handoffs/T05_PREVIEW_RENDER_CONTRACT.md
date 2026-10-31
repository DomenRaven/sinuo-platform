# T05 栅格与 PDF 预览合同

版本：`T05-PREVIEW-RENDER-1`。日期：2026-10-31。Pillow 和 pypdfium2 进入主依赖。Blender 不安装。

保留 `preview_disposition` 和 `run_preview_converter` 的现有结果。真实渲染用新函数，不把注入转换器的正常返回改成已生成预览。

栅格只接受 Pillow 能解码的非 SVG 图片。像素数超过调用方给出的上限就拒绝，不写预览。输出是 PNG，不保留 EXIF。PDF 只渲染第一页，输出 PNG，同样受像素上限。GLB 在没有 Blender 时保持 `unavailable`，不写文件，不用占位图。

派生文件必须落在调用方给出的派生根里面。路径段拒绝 `..`。元数据带生成器名称和版本。原件字节不被改写。
