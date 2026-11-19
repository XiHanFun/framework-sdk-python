#!/usr/bin/env bash
# 发版核对（RFC-0024「实现钉规范」）：打标签之前在本地跑一遍，推送标签后 release.yml 的 guard 再跑同一份。
#
# 用法：scripts/release-check.sh <标签> [<提交>]
#   提交缺省为标签指向的提交；标签还没打时为 HEAD。核对的是那个提交里的文件，不是工作区。
#
# 缺一不发：
#   ① 标签是 v<语义化版本>，带预发布后缀（v0.1.0-rc.1）的是预发布；
#   ② 提交在 origin/main 的历史里：预发布与正式版都从 main 发；
#   ③ 标签号对应提交里 src/xihan_framework/__init__.py 的 __version__（构建后端与 primitives.SDK_VERSION 都取它）。
#      标签按语义化版本写，与其他 SDK 一致；__version__ 按 PEP 440 的规范写法，因为 tests/test_package.py 要求它等于
#      分发元数据里规范化之后的版本：v0.1.0 对 0.1.0，v0.1.0-alpha.N / -beta.N / -rc.N 对 0.1.0aN / 0.1.0bN / 0.1.0rcN；
#   ④ spec 子模块正好在规范的标签上：预发布可钉规范的 vX.Y.Z-rc.N，正式版只钉规范的正式版标签。
#
# 推上去的标签 guard 不通过时 release.yml 不会发布，但标签得删掉重打，所以打标签之前先在本地跑通。
set -euo pipefail

spec_remote="${SPEC_REMOTE:-https://github.com/XiHanFun/framework-spec.git}"

fail() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::error::$1" >&2
  else
    echo "不通过：$1" >&2
  fi
  exit 1
}

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "用法：$0 <标签> [<提交>]" >&2
  exit 2
fi
tag="$1"
if [ $# -eq 2 ]; then
  commit="$(git rev-parse --verify --quiet "$2^{commit}")" || fail "找不到提交 $2。"
elif git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
  commit="$(git rev-parse "$tag^{commit}")"
else
  commit="$(git rev-parse HEAD)"
fi

# ① 标签的写法
if ! printf '%s' "$tag" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'; then
  fail "标签 $tag 不是 v<语义化版本>（如 v0.1.0、v0.1.0-rc.1）。"
fi
version="${tag#v}"
case "$version" in
  *-*) prerelease=true ;;
  *) prerelease=false ;;
esac
echo "通过：标签 $tag（$([ "$prerelease" = true ] && echo 预发布 || echo 正式版)），核对提交 $commit"

# ② 只从 main 发：标签本身不带分支信息，查祖先关系
git fetch --quiet --no-tags origin "+refs/heads/main:refs/remotes/origin/main" || fail "取不到 origin 的 main。"
if ! git merge-base --is-ancestor "$commit" refs/remotes/origin/main; then
  fail "提交 $commit 不在 main 的历史里。先把要发布的提交快进到 main，再在上面打标签。"
fi
echo "通过：提交在 main 的历史里"

# ③ 标签号与 __version__ 一致：先把标签的语义化版本换成 PEP 440 的规范写法再比；
#    PEP 440 只有 a、b、rc 三种预发布，别的后缀（如 -preview.1、-rc）写不出对应的版本，直接拒绝
expected="$version"
if [ "$prerelease" = true ]; then
  core="${version%%-*}"
  suffix="${version#*-}"
  case "$suffix" in
    alpha.*) label=a ;;
    beta.*) label=b ;;
    rc.*) label=rc ;;
    *) label= ;;
  esac
  number="${suffix#*.}"
  if [ -z "$label" ] || ! printf '%s' "$number" | grep -Eq '^(0|[1-9][0-9]*)$'; then
    fail "Python SDK 的预发布标签只认 -alpha.N、-beta.N、-rc.N（对应 PEP 440 的 aN、bN、rcN），$tag 的后缀 $suffix 写不成 PEP 440 的版本。"
  fi
  expected="$core$label$number"
fi
declared="$(git show "$commit:src/xihan_framework/__init__.py" | sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
if [ "$declared" != "$expected" ]; then
  fail "标签 $tag 对应的 __version__ 应是 $expected（PEP 440 的规范写法），src/xihan_framework/__init__.py 里是 ${declared:-没读到}。先把版本改成 $expected 并快进到 main，再打标签。"
fi
echo "通过：__version__ 是 $expected"

# ④ spec 子模块钉在规范标签上：读树里记的指针，到规范仓库的远端标签里找指向它的；
#    附注标签看剥开后的 ^{} 行，conformance/ 前缀的 Go 模块标签不算
pinned="$(git ls-tree "$commit" spec | awk '$2 == "commit" { print $3 }')"
[ -n "$pinned" ] || fail "提交里没有 spec 子模块。"
remote_tags="$(git ls-remote --tags "$spec_remote")" || fail "取不到规范仓库 $spec_remote 的标签。"
spec_tags="$(printf '%s\n' "$remote_tags" \
  | awk -v sha="$pinned" '$1 == sha { name = $2; sub(/^refs\/tags\//, "", name); sub(/\^\{\}$/, "", name); print name }' \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$' | sort -u || true)"
if [ "$prerelease" = false ]; then
  spec_tags="$(printf '%s\n' "$spec_tags" | grep -v -- '-' || true)"
fi
if [ -z "$spec_tags" ]; then
  fail "spec 指向的规范提交 $pinned 上没有可用的规范标签（预发布要 vX.Y.Z 或 vX.Y.Z-rc.N，正式版要 vX.Y.Z）。先给规范打标签并把 spec 指到那个标签，再发版。"
fi
spec_tag="$(printf '%s\n' "$spec_tags" | tail -n 1)"
echo "通过：spec 在规范标签 $(printf '%s\n' "$spec_tags" | tr '\n' ' ')上"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "version=$version"
    echo "prerelease=$prerelease"
    echo "spec_tag=$spec_tag"
  } >>"$GITHUB_OUTPUT"
fi
echo "全部通过：可以发布 $tag"
