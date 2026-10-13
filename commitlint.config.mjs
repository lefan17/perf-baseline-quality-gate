/**
 * commitlint 配置（Conventional Commits）。
 *
 * ## 为什么默认不启用它
 *
 * 仓库里同时给了 `scripts/check_commit_msg.py`，两者规则刻意保持等价。
 * 默认走那个 Python 实现，理由写在它的文件头：这是个纯 Python 仓库，
 * CI 里一个 Node 都没装，为了一个提交信息检查引入第二条工具链不划算。
 *
 * 但本机**确实**装了 Node（实测 `node --version` → v24.16.0，
 * `npx --version` → 11.13.0），所以这条路是走得通的。要用：
 *
 *     npx --yes @commitlint/cli@19 --edit .git/COMMIT_EDITMSG
 *
 * 或装成钩子（会覆盖 .pre-commit-config.yaml 里那条 local hook）：
 *
 *     npm install --save-dev @commitlint/cli @commitlint/config-conventional
 *     npx husky init && echo 'npx --yes commitlint --edit "$1"' > .husky/commit-msg
 *
 * ## 两条路线的等价性
 *
 * 改这里任何一条规则，都必须同步改 scripts/check_commit_msg.py ——
 * 否则"同一个提交在本地过、在别人的机器上不过"。
 * 两边共有的规则：type-enum、header 格式、header 长度上限 100、
 * subject 不以标点结尾。Python 那边额外拦"标题与正文之间必须有空行"，
 * 因为 git 靠这个空行区分 subject 与 body（commitlint 靠 parser 处理，
 * 不需要单独一条）。
 */
export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    // 与 scripts/check_commit_msg.py 的 TYPES 一一对应。
    'type-enum': [
      2,
      'always',
      [
        'feat',
        'fix',
        'docs',
        'style',
        'refactor',
        'perf',
        'test',
        'build',
        'ci',
        'chore',
        'revert',
      ],
    ],
    'type-case': [2, 'always', 'lower-case'],
    'type-empty': [2, 'never'],
    'scope-case': [2, 'always', 'lower-case'],
    'subject-empty': [2, 'never'],
    // 上限 100 与 Python 实现一致：默认的 72 对中文过严
    // （一个汉字在 git log 里占两列宽，72 列只够 36 个汉字，写不清"为什么"）。
    'header-max-length': [2, 'always', 100],
    // 中文没有大小写概念，所以只对 ASCII 提醒。
    'subject-case': [0],
    'body-max-line-length': [0],
    'footer-max-line-length': [0],
  },
};
