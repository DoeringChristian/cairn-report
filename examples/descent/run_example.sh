#!/bin/sh
# The whole example in order: dataset -> run -> report, each published as an artifact, then the UI.
# Run from this folder with Cairn installed (with matplotlib); everything lands in .cairn/.
set -eu
cd "$(dirname "$0")"

cairn publish data/quadratic datasets/quadratic   # the dataset becomes datasets/quadratic@v1
python descent.py                                  # a run that pulls it (and records that)
cairn publish runs/descent/latest descent          # the run becomes descent@v1, its input recorded
cairn publish reports/descent-study                # builds the report and stores it with its pinned inputs
cairn show descent@latest                          # the version, its files, inputs, and consumers
echo
echo "Explore it: cairn ui   (then open the printed address)"
