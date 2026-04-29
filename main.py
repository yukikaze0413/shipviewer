"""
工业模型查看器 - ShipViewer
基于 PySide6 + VTK 的工业级3D模型查看器
支持 .3dm (Rhino) 和 .glb (glTF) 格式
"""
import sys
import os

# 设置环境变量，确保高DPI支持
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QLocale
from PySide6.QtGui import QFont, QIcon

from core.paths import app_path, resource_path


def configure_vtk_output():
    """Redirect VTK warnings away from the Windows popup output window."""
    import vtk

    log_dir = app_path("logs")
    os.makedirs(log_dir, exist_ok=True)

    output_window = vtk.vtkFileOutputWindow()
    output_window.SetFileName(os.path.join(log_dir, "vtk-output.log"))
    vtk.vtkOutputWindow.SetInstance(output_window)


def main():
    """应用程序入口"""
    configure_vtk_output()

    from core.main_window import MainWindow

    app = QApplication(sys.argv)

    # 设置应用程序信息
    app.setApplicationName("工业模型查看器")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("ShipViewer")

    # 设置中文区域
    locale = QLocale(QLocale.Chinese, QLocale.China)
    QLocale.setDefault(locale)

    # 设置默认字体
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 加载样式表
    style_path = resource_path("styles", "dark_theme.qss")
    if os.path.exists(style_path):
        with open(style_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
