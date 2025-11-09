#!/bin/bash

ls -lR /eos/experiment/ship/user/ammagnan/TargetProd/$1/ | awk '
/:$/ {dir=$0; sub(":","",dir); next}
NF >= 5 && $1 !~ /^d/ {
  sum[dir]+=$5
  total+=$5
}
END {
  for (d in sum)
    printf "%s: %.2f MB\n", d, sum[d]/1024/1024
  printf "Total: %.2f MB\n", total/1024/1024
}'
