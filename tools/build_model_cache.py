"""预生成 ShipViewer 的 .3dm 几何缓存。"""

import argparse
import os
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.model_loader import ModelLoadThread, model_cache_key, model_cache_root


def build_cache(filepath):
    filepath = os.path.normpath(os.path.abspath(filepath))
    if not os.path.exists(filepath):
        raise FileNotFoundError(filepath)
    if os.path.splitext(filepath)[1].lower() != ".3dm":
        raise ValueError("目前只支持生成 .3dm 模型缓存。")

    loader = ModelLoadThread(filepath)
    loader.force_rebuild_cache = True

    started = time.perf_counter()
    actors = loader._load_3dm(filepath)
    elapsed = time.perf_counter() - started
    if not actors:
        raise RuntimeError("没有生成可渲染对象。")

    cache_dir = os.path.join(model_cache_root(), model_cache_key(filepath))
    print(f"模型: {filepath}")
    print(f"对象数: {len(actors)}")
    print(f"缓存目录: {cache_dir}")
    print(f"耗时: {elapsed:.2f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+", help="需要生成缓存的 .3dm 模型文件")
    args = parser.parse_args()

    for model_path in args.models:
        build_cache(model_path)


if __name__ == "__main__":
    main()
