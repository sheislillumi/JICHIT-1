# 展示会・商談会 公募案件ダッシュボード

47都道府県庁 ＋ 指定団体（埼玉県産業振興公社、日本政策金融公庫、関西広域連合、滋賀県商工会連合会、JA全農）の
入札公告・公募情報ページを毎日自動巡回し、「展示会・商談会の開催運営業務」に関連しそうな公募案件だけを
抽出して一覧表示する社内向けダッシュボードです。

- 収集: GitHub Actions が毎日 06:00 JST に `scraper/scrape.py` を実行し、`data/listings.json` を更新・コミット
- 閲覧: GitHub Pages で公開される `index.html`（フィルタ・検索・ソート機能つき）
- 団体追加: ダッシュボード右上の「＋ 団体を追加」から、GitHub Personal Access Token を使って
  `config/organizations.json` に直接コミットできます

## セットアップ手順（初回のみ）

### 1. GitHubにリポジトリを作成してpush

このフォルダをそのまま新規リポジトリの中身として使ってください（VS Code + Claude Code から実行する場合の例）。

```bash
cd jichitai-kobo-dashboard
git init
git add .
git commit -m "init: 展示会・商談会 公募案件ダッシュボード"
git branch -M main
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

すでに `git init` 済みの場合は `git init` はスキップしてください。

### 2. GitHub Pagesを有効化

1. リポジトリの Settings → Pages
2. Source: `Deploy from a branch`
3. Branch: `main` / `/ (root)` を選択して Save

数分後に `https://<owner>.github.io/<repo>/` でダッシュボードが公開されます。

### 3. GitHub Actionsの権限を確認

1. リポジトリの Settings → Actions → General
2. 「Workflow permissions」で **Read and write permissions** を選択して Save
   （`daily-scrape.yml` が `data/listings.json` をコミットするために必要です）

### 4. 初回の手動実行（任意）

毎日06:00 JSTの自動実行を待たずに今すぐ試したい場合は、
Actions タブ → `Daily Scrape` → `Run workflow` から手動実行できます。

## 「団体を追加」機能について

ダッシュボードの「＋ 団体を追加」ボタンから、新しい団体（市区町村・団体など）を
`config/organizations.json` に追加できます。この機能はブラウザから直接 GitHub REST API を叩いて
ファイルをコミットする仕組みのため、以下のトークンが必要です。

1. GitHub の Settings → Developer settings → Personal access tokens → **Fine-grained tokens** で新規作成
2. Repository access: **このリポジトリのみ** を選択（トークンの権限を最小化するため）
3. Permissions: **Contents = Read and write** を付与
4. 発行したトークンをダッシュボードの「GitHub連携設定」に貼り付けて保存

トークンはブラウザの localStorage にのみ保存され、外部には送信されません（GitHub API に直接送られるのみ）。
社内共有する場合は、担当者ごとに個別のトークンを発行することを推奨します。

団体追加後、コミットが `config/organizations.json` を変更するため daily-scrape ワークフローが
自動トリガーされ、次のスクレイプで新しい団体が収集対象になります（すぐに反映したい場合は Actions から手動実行）。

## ディレクトリ構成

```
jichitai-kobo-dashboard/
├── .github/workflows/daily-scrape.yml   # 日次スクレイプ + データコミット
├── config/organizations.json            # 収集対象団体の一覧（ダッシュボードからも編集可）
├── scraper/
│   ├── scrape.py                        # 汎用スクレイパー本体
│   └── requirements.txt
├── data/
│   ├── listings.json                    # 収集結果（履歴保持、ダッシュボードが参照）
│   └── scrape_log.json                  # 収集ステータス（団体ごとの成否）
├── index.html / assets/app.js / assets/style.css  # ダッシュボード本体（GitHub Pages）
└── README.md
```

## 抽出ロジックについて（重要な限界事項）

対象が50団体を超えるため、サイトごとに専用パーサーを作り込むのではなく、汎用ロジックで運用しています。

- ページ内の全リンクとその周辺テキスト（li/tr/p/div等）を取得
- 「展示会」「商談会」「見本市」などのキーワード **かつ** 「運営」「委託」「プロポーザル」「公募」などの
  キーワードの両方を含む箇所だけを抽出（`scraper/scrape.py` の `KEYWORDS_TOPIC` / `KEYWORDS_OPERATION` で編集可）

この方式には以下の限界があります。

- **JavaScriptで描画されるページ・フレーム構成のページ・要ログインの電子入札システムは取得できません。**
  `config/organizations.json` の各団体の `note` に注意点を記載しているので、
  取得できない団体が判明した場合は当該団体の入口ページURLを別途調整してください（例: 一覧がPDFのみ、
  検索フォーム経由でしか出せない、等は現状のスクレイパーでは対応不可）。
- キーワードに一致しない表現（例:「展示商談イベント」等の言い回し）は拾えません。必要に応じて
  `scraper/scrape.py` のキーワードリストに追加してください。
- 逆に、キーワードが偶然両方含まれる無関係な案件を誤って拾う場合もあります（人手での最終確認を推奨）。
- `data/scrape_log.json` に団体ごとの取得成否が記録されるので、ダッシュボードの「収集ステータス」で
  エラーが出ている団体は定期的に確認してください。

## ローカルでの動作確認

```bash
cd scraper
pip install -r requirements.txt
python scrape.py
```

`data/listings.json` と `data/scrape_log.json` が更新されます。その後、`index.html` を
ブラウザで直接開く（または `python -m http.server` 等でローカル配信）と確認できます。
