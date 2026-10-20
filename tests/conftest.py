import os
import sys

# 저장소 루트(wan 패키지)와 tests/(시험 도구)를 import 경로에 추가
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
