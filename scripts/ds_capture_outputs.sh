#!/usr/bin/env bash
# 重新抓一遍 README「P3」章节里所有命令的真实输出（用于核对 README，不写入仓库）
# 用法：bash scripts/ds_capture_outputs.sh [输出目录，默认 /tmp/ds_out]
set -u
cd "$(dirname "$0")/.."
OUT="${1:-/tmp/ds_out}"
mkdir -p "$OUT"

run() {  # run <名字> <python 参数...>
  local name="$1"; shift
  env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY .venv/bin/python "$@" >"$OUT/$name.out" 2>"$OUT/$name.err"
  echo "[$name] rc=$? bytes=$(wc -c <"$OUT/$name.out")"
}

run setup       scripts/ds_setup_demo.py
run sync        -m lineage.cli ds sync --graph-out ds_lineage.json
run workflows   -m lineage.cli ds workflows
run tables      -m lineage.cli ds tables ods.ods_卷烟产量流水
run task        -m lineage.cli ds task wf_dws_汇总
run upstream    -m lineage.cli ds upstream ads.ads_经营指标驾驶舱 --depth 3
run export      scripts/ds_export_table_graph.py ds_lineage.json /tmp/ds_table_graph.json
run stats       -m lineage.cli stats  --graph /tmp/ds_table_graph.json
run impact      -m lineage.cli impact cdw.dws_产销存汇总 --graph /tmp/ds_table_graph.json --depth 3
run viz_html    -m lineage.cli viz    /tmp/ds_table_graph.json --format html \
                    --out docs/ds_lineage.html --highlight ads.ads_经营指标驾驶舱 --depth 4
run viz_mermaid -m lineage.cli viz    /tmp/ds_table_graph.json --format mermaid --out docs/ds_lineage.mmd
ls -lh ds_lineage.json docs/ds_lineage.html docs/ds_lineage.mmd
