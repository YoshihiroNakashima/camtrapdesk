# camtrapdesk

カメラトラップ調査のSDカードを、挿すだけで取り込み・検証・NAS転送し、野帳の対応表で地点名に付け替え、検証済みのカードだけ安全に消すためのデスクトップアプリ（Windows / macOS）。R パッケージ [camtrapdata](https://github.com/YoshihiroNakashima/camtrapdata) の SDカード系の関数（`rename_sd_volume`, `copy_from_sd`, `check_copy`, `delete_sd`, `rename_sd_to_station`）を GUI に移植し、台帳（SQLite + manifest.json）とハッシュ検証を加えたもの。

## 画面と流れ

左のメニューは、やる頻度で3つに分かれている。

**最初に一度だけ**
- **カードにIDを付ける** — 新しいSDカードの直下に id.txt（カードの番号）を書き込む。番号はカードに貼ったラベルと同じにする。任意で、ボリューム名（エクスプローラーに出る名前）もIDに合わせられる。

**リーダーを挿し替えたら**
- **スロットに名前を付ける** — リーダーの差し口ごとに名前（1-A、U1-3-B など）を付ける。ドライブ文字（E:, F:, …）が変わっても同じ名前で表示され、名前の最後の区切りより前が同じものは同じリーダーとみなして、コピーが1台に偏らないよう順番を組む。

**移行のたびに（この順で）**
1. **取り込み** — カードを挿すと自動で検出し、並列にコピーしてハッシュで検証。完了したカードは「抜いてOK」になる。ドライブ文字に依存しないので、20スロットを超えても扱える（Windows はボリュームGUIDパスで列挙）。検証済みのフォルダはバックグラウンドで NAS へコピーして読み戻し検証し、NAS に届かないときは待って再試行、再起動後も続きから。中継のコピーは既定で残す（控え）。中継の空きが足りないカードは「空き待ち」になり、別のSSDを繋いで「中継先を変更…」で選ぶとそちらへ続きから取り込む（コピー中にいっぱいになった場合も同様）。以前の中継SSDに残った分の NAS 転送は、そのSSDが繋がっているあいだに続き、ドライブ文字が変わっても中継先フォルダの目印（`.camtrapdesk-root.json`）で見つけ直す。中継を小さくしたいときは設定で「NASで検証済みになったカードは中継のコピーを自動で消す」をオンにできる。
2. **地点名に変更** — 野帳（Excel）を読み、使う行（Type と Season）と変更するフォルダ（通常は取り込み画面で指定した今回のシーズンフォルダ）を選んでドライラン。回収SD（RetrievalSD）のカードID→地点名の対応に重複があれば保留にして一覧に出し、曖昧でない分だけ変更する。中継と NAS の両方を見て、ある方を変える。列名の大文字小文字や綴りの揺れ（Statuion など）は自動で読み替える。
3. **カードを消す** — 挿入中のカードを台帳と突合し、NAS で検証済みのファイルだけを削除する。id.txt や台帳に無いファイルは残す。

**記録・設定**
- **台帳** — 取り込み・転送・削除の記録。保存先の manifest.json から再構築できる（別PCで取り込んだ分の取り込みにも使う）。
- **設定** — 保存先、NAS、カード内の対象フォルダ、野帳の列名、地点名の付け方（接頭辞と桁数）など。移行のたびに変わるもの（シーズンフォルダ名、野帳ファイル）は各画面で指定し、設定画面には置いていない。

フォルダ構造は `保存先/<シーズン>/<カードID>/*.mp4` → 名前変更後 `保存先/<シーズン>/<地点名>/*.mp4`。シーズンフォルダ名を空にすると `保存先/<カードID>/` になる。各フォルダに `manifest.json`（ファイル名・サイズ・ハッシュ・検証日時）が置かれる。

## 他の調査・研究室で使うとき

既定値は特定の調査に依らない。必要最小限は、保存先（中継）と、野帳の **Station** 列と **回収SD** 列だけ。

- 野帳に **Season** 列や **Type** 列が無くてもよい（無ければ全行が対象になる）。あれば「地点名に変更」で選べる。
- シーズンフォルダ名は任意。空なら保存先の直下にカードごとのフォルダができる。
- 地点名の付け方は設定の「接頭辞」と「桁数」で決める。既定は接頭辞 `BSN`、4桁（野帳の 482 → `BSN0482`）。接頭辞を空にすると数字だけ（`0482`）、桁数を 0 にすると書かれたまま。野帳の Station が番号でない文字列（Ridge-A など）はそのまま使う。
- カード内の対象フォルダは既定 `DCIM/100_BTCF`（Browning）。そのフォルダが無いカードは `DCIM` 以下をすべて探すので、他機種でもそのまま動く。設定で変えられる。
- NAS が無ければ NAS 欄を空にする。保存先がそのまま最終保存先になり、削除の条件も保存先での検証済みになる。

## 配布と起動

### 学生に配る（Python 不要）

GitHub の Releases ページから、OS に合うものを 1 つダウンロードする。

- **Windows**: `camtrapdesk-windows.exe`。好きな場所に置いてダブルクリックするだけ。初回は Windows の SmartScreen が「WindowsによってPCが保護されました」と出すので、「詳細情報」→「実行」。1 ファイルに全部入っているので、起動に数秒かかる（展開のため）。
- **Mac（M1 以降）**: `camtrapdesk-macos-apple-silicon.zip`。展開して `camtrapdesk.app` を「アプリケーション」に入れる。
- **Mac（Intel）**: `camtrapdesk-macos-intel.zip`。同じ手順。

Mac は署名していないため、初回に「開発元を確認できないため開けません」と出る。閉じてから「システム設定」→「プライバシーとセキュリティ」を下にスクロールし、「"camtrapdesk" は…ブロックされました」の横の「このまま開く」を押す（macOS 15 以降は右クリック→「開く」では通らない）。ターミナルで `xattr -dr com.apple.quarantine /Applications/camtrapdesk.app` としてもよい。毎回この手間を無くすには Apple Developer Program（年額）で署名と公証をする。

設定と台帳は PC ごとに `%APPDATA%\camtrapdesk`（Mac は `~/Library/Application Support/camtrapdesk`）に入る。複数の PC で取り込んだ分は、保存先の manifest.json から台帳を再構築すれば 1 台にまとめられる（「台帳」画面）。新しい版を配るときは、新しいタグを付けて Release を作り、学生にダウンロードし直してもらう。

### Release の作り方（開発者）

1. GitHub にリポジトリを作り、この内容を push する（`.venv` や `__pycache__` は `.gitignore` 済み）。
2. バージョンを上げる（`camtrapdesk/__init__.py` と `pyproject.toml`）。
3. `git tag v0.1.0 && git push origin v0.1.0`。GitHub Actions（`.github/workflows/build.yml`）がテストを実行し、Windows の exe と Mac 2 種（Apple Silicon / Intel）の app を作って、Releases に添付する（15〜20 分）。
4. 配布ページの URL（`https://github.com/<ユーザー名>/camtrapdesk/releases/latest`）を学生に知らせる。

push だけでも（タグ無し）テストとビルドは走り、Actions の画面の Artifacts から取れる。手元で作る場合:

```bash
pip install -e ".[dev]"
pyinstaller --noconfirm --clean --windowed --onefile --name camtrapdesk --paths . --collect-all openpyxl tools/entry.py   # Windows
pyinstaller --noconfirm --clean --windowed --name camtrapdesk --paths . --collect-all openpyxl tools/entry.py             # Mac → dist/camtrapdesk.app
```

### フォルダごと渡して起動する（Python が入っている PC）

- Windows: `起動.bat` をダブルクリック。初回だけ、フォルダ内に `.venv` を作って PySide6 などを取得する（数分。進み具合は黒い画面に出る）。2回目以降はすぐ起動する。Python 3.9 以上が入っていない場合はその旨が表示され python.org が開くので、インストール（「Add python.exe to PATH」にチェック）後にもう一度実行する。
- macOS: `起動.command` をダブルクリック。

実体は `tools/launch.py` で、表示は Python 側で行うのでコンソールの文字コードに左右されない。

### 開発環境

```bash
pip install -e ".[dev]"
python -m camtrapdesk
QT_QPA_PLATFORM=offscreen python -m pytest -q   # テスト
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

`read_bs`, `apply_species_corrections`, `time_correct_ff`, `time_correct_order`, `plot_survey_effort`, `sample_and_copy_videos`, `add_videos`, `collapse_strata` は後の段階で別のセクションとして追加する（いまのメニューには出していない）。
