import os, sys, pathlib
os.environ["PENNY_AI"] = "off"  # tests never call a real model; the AI tests stub brain.decide
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
