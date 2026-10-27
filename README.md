# camtrapdesk

カメラトラップ調査のSDカードを、挿すだけで取り込み・検証・NAS転送し、野帳の対応表で地点名に付け替え、検証済みのカードだけ安全に消すためのデスクトップアプリ（Windows / macOS）。R パッケージ [camtrapdata](https://github.com/YoshihiroNakashima/camtrapdata) の SDカード系の関数（`rename_sd_volume`, `copy_from_sd`, `check_copy`, `delete_sd`, `rename_sd_to_station`）を GUI に移植し、台帳（SQLite + manifest.json）とハッシュ検証を加えたもの。

## 画面と流れ

1. **取り込み** — アプリを起動してカードを挿すと自動で検出し、並列にコピーしてハッシュで検証。完了したカードは「抜いてOK」になる。ドライブレターに依存しないので、20スロットを超えても扱える（Windows はボリュームGUIDパスで列挙）。
2. **NASへの転送** — 中継先で検証済みのフォルダを、バックグラウンドでNASへコピーして読み戻し検証。NASに届かないときは待って再試行し、再起動後も続きから。
3. **名前変更（地点名へ）** — 野帳（Excel）を読み、Type と Season を選んでドライラン。回収SD（RetrievalSD）のカードID→地点名の対応に重複があれば保留にして一覧に出し、曖昧でない分だけ変更する。列名の大文字小文字や綴りの揺れ（Statuion など）は自動で読み替える。
4. **削除** — 挿入中のカードを台帳と突合し、NASで検証済みのファイルだけを削除する。id.txt や台帳に無いファイルは残す。
5. **台帳** — 取り込み・転送・削除の記録。保存先の manifest.json から再構築できる（別PCで取り込んだ分の取り込みにも使う）。
6. **設定** — 保存先、NAS、対象フォルダ（既定 `DCIM/100_BTCF`）、野帳の列名（ファイル自体は名前変更画面で選ぶ）、Type ごとのフォルダ名の整形ルール（既定: N → `N260710` / `BSN0482`）。

フォルダ構造は `保存先/<シーズン>/<カードID>/*.mp4` → 名前変更後 `保存先/<シーズン>/<地点名>/*.mp4`。各フォルダに `manifest.json`（ファイル名・サイズ・ハッシュ・検証日時）が置かれる。

## 起動（ダブルクリック）

- Windows: `起動.bat` をダブルクリック。初回だけ、フォルダ内に `.venv` を作って PySide6 などを取得する（数分。進み具合は黒い画面に出る）。2回目以降はすぐ起動する。Python 3.9 以上が入っていない場合はその旨が表示され python.org が開くので、インストール（「Add python.exe to PATH」にチェック）後にもう一度実行する。
- macOS: `起動.command` をダブルクリック（初回に「開発元を確認できない」と出たら右クリック→「開く」）。

実体は `tools/launch.py` で、表示は Python 側で行うのでコンソールの文字コードに左右されない。学生のPCに配る場合は、GitHub Actions が作る `camtrapdesk.exe`（Python 不要）を使う。

## 使い方（開発環境）

```bash
pip install -e ".[dev]"
python -m camtrapdesk
```

テスト:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

配布用の実行ファイルは GitHub Actions（`.github/workflows/build.yml`）が Windows の `camtrapdesk.exe` と macOS の `camtrapdesk.app` を作る。`v0.1.0` のようなタグを付けて push すると Release に添付される。macOS では署名していないため、初回は右クリック→「開く」が必要。

ローカルで作る場合:

```bash
pip install pyinstaller
pyinstaller --windowed --onefile --name camtrapdesk --collect-all openpyxl camtrapdesk/__main__.py
```

## 設定ファイルと台帳の場所

- Windows: `%APPDATA%\camtrapdesk\settings.json`, `ledger.sqlite`
- macOS: `~/Library/Application Support/camtrapdesk/`
- 環境変数 `CAMTRAPDESK_HOME` で変更できる。

## 運用上の注意

- Windows の自動再生（AutoPlay）は切っておく（カードを20枚挿すと20個のダイアログが出る）。
- 取り込み中・夜間転送中はPCがスリープしないようにする。
- NAS はネットワークドライブの文字を割り当てず、`\\NAS名\共有名\...` のUNCパスを設定に書く。資格情報は Windows の「資格情報マネージャー」に一度登録しておく。
- 削除は「NASで検証済み」が既定の条件。NASを使わない運用（保存先をそのまま最終保存先にする）では、NAS欄を空にすると保存先での検証済みが条件になる。

## 今後（データ管理側）

`read_bs`, `apply_species_corrections`, `time_correct_ff`, `time_correct_order`, `plot_survey_effort`, `sample_and_copy_videos`, `add_videos`, `collapse_strata` を「データ管理」セクションに順次移植する。
