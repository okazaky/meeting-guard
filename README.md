# meeting-guard

Zoom / Google Meet の会議を「気づくまで」段階的に知らせる macOS 常駐アラームです。
カレンダー通知を見落としやすい人（ADHD などで時間の感覚が途切れやすい人を含む）向けに、
通知を少しずつ強くし、最後は会議を自動で開き、それでも参加していなければ電話をかけます。

作者: okazaky ／ ライセンス: MIT

---

## できること

launchd が 1 分ごとにスクリプトを起動し、Google カレンダーの直近の予定を見て次の順に知らせます。

| タイミング | 動作 |
|---|---|
| 10 分前 | スマホへプッシュ通知（ntfy・設定したときだけ） |
| 5 分前 | Mac の音声読み上げ（`say`）＋プッシュ |
| 1 分前 | 音声＋ダイアログ（お知らせだけ。押しても警報は止まりません） |
| 開始時刻 | Zoom（アプリで直接）／ Meet のリンクを自動で開く＋音声＋プッシュ |
| 開始後 | 「参加します（警報停止）」ボタンを押すか、Zoom の会議参加を検知するまで、約 2 分ごとに音声＋ダイアログ＋プッシュを繰り返す（最大 `nag_minutes` 分） |
| 開始 2 分後 | まだ参加していなければ、自分の携帯へ電話（Twilio・任意・回数制限あり） |

### この設計にしている理由

- **だんだん強くする**: 最初から大音量だと慣れて無視してしまうため、段階を分けています。
- **開始前のダイアログでは止まらない**: 反射的にダイアログを閉じても警報が消えないように、止められるのは開始後の「参加します」ボタンだけです。
- **取りこぼしても 1 回だけ鳴らす**: スリープ明けなどで複数の段階を過ぎていても、いちばん差し迫った 1 段だけを鳴らします（連続で鳴りません）。
- **リンクのない予定は追いかけない**: Zoom / Meet のリンクがない予定（対面の用事など）は開始前のお知らせと開始時刻の音声だけで、開始後の繰り返しや電話はしません。
- **承認していない招待は無視する**: 他人が送りつけた招待で警報や電話が動かないよう、既定では「自分が作った予定」「自分が承認（出席）した招待」「参加者のいない予定」だけを対象にします（`include_unaccepted`）。
- **電話は既定で無効です**（`call.enabled: false`）。自分で有効にしたときだけ発信します。
- **電話には上限がある**: 1 件の予定につき最大 1 回、失敗しても再発信しません。さらに全体で 1 日 `call.max_per_day` 回（既定 2）、前回から `call.min_interval_min` 分（既定 30）空いていないと発信しません。上限で見送った予定は「発信済み」にはせず、上限が解けた次の起動で（開始後 `nag_minutes` 以内なら）発信します。
- **1 回の起動で鳴らす予定は最大 `max_events_per_run` 件（既定 3）**: 開始時刻が近い順です。
- **予定を移動しても鳴らし直さない**: 鳴らした段階・電話・「参加します」の記録は予定 ID ごとに保存し、開始時刻が変わっても消しません。例外として、スリープ明けなどで「開始から `nag_minutes` を過ぎて初めて見えたため鳴らさなかった」予定が、まだ一度も鳴らしていないまま別の時刻へ移動され、新しい開始がこれからか、過ぎていても `nag_minutes` 以内のときは、段階を最初からやり直します。新しい開始がすでに過ぎていれば、その場で会議を開き、催促と電話の判定もふだんどおり行います（リンクのない予定は開始時刻の音声だけ）。電話と「参加します」の記録は残します。
- **警報を止めかねない問題は、黙らずに知らせる**: 電話の処理で何が起きても、音声・ダイアログ・プッシュは先に鳴らします。一部のカレンダーが読めない、予定が多すぎて読み切れない、Google の認可が切れた、設定ファイルに誤りがある、会議を開けなかった、といったときは Mac の音声とダイアログで知らせます（同じ種類の知らせは 1 時間に 1 回まで）。

---

## 必要なもの

- macOS（`say`、`osascript`、`open`、launchd を使います）
- python3（3.9 以上。標準ライブラリだけで動きます）
  - macOS 付属の `/usr/bin/python3` は 3.9 で、本家のサポートは終了しています（EOL）。動作はしますが、新しい python3 があれば `PYTHON=/path/to/python3 ./install.sh` で指定することをおすすめします。
- Google Cloud の OAuth クライアント（**種類は「デスクトップ アプリ」**）と Google Calendar API の有効化
- （任意）スマホに [ntfy](https://ntfy.sh) アプリ
- （任意）Twilio アカウント。**電話の発信と電話番号の維持に料金がかかります**

---

## セットアップ

### 1. インストール

```bash
git clone <このリポジトリ>
cd meeting-guard
./install.sh
```

`install.sh` は次のことをします。

- 保存先 `~/.local/share/meeting-guard/`（`MEETING_GUARD_HOME` で変更可）を検査してから作成します。`$HOME` の中であること、`$HOME` そのものでないこと、シンボリックリンクでないこと、空か meeting-guard が作った印（`.meeting-guard-home`）があることを確かめ、満たさないときは何も変えずに止まります。
- スクリプト（`meeting_guard.py` と `mg_*.py`）をコピーし、保存先と `~/.config/meeting-guard/` を権限 700 にして印ファイルを置きます。
- `config.json` と `secrets.env`（権限 600、中身は空欄）を、**まだ無いときだけ**作成します。プッシュ通知と電話は、自分で値を書くまで動きません。
- launchd の設定ファイル（plist）を作り直し、前の版は `.bak` に残してから入れ替え、`io.github.meeting-guard` として起動します。plist には `Umask`（ファイルを本人だけが読める権限で作る）と `MEETING_GUARD_HOME` が入ります。
- 最後に、ntfy のトピック名の候補（その場で作ったランダムな文字列）を表示します。

### 2. Google Cloud の準備

> **注意: 7 日で認可が切れる設定があります。**
> OAuth 同意画面の「公開ステータス」が **「テスト中（Testing）」のままだと、Google は refresh token を 7 日で失効させます**。失効すると会議の警報が止まります（meeting-guard は `invalid_grant` を検知し、「--auth を実行してください」と音声とダイアログで知らせます）。
> 防ぐには、OAuth 同意画面で公開ステータスを **「本番環境（In production）」に切り替えて**ください。自分だけで使うなら Google の審査（verification）は不要で、「未確認のアプリ」と表示されたまま使えます。切り替えない場合は、7 日ごとに `--auth` をやり直してください。

1. Google Cloud Console でプロジェクトを作り、「Google Calendar API」を有効にします。
2. 「OAuth 同意画面」を設定し、自分のアカウントをテストユーザーに追加します（上の注意のとおり、公開ステータスは「本番環境」に切り替えることをおすすめします）。
3. 「認証情報」→「OAuth クライアント ID を作成」→ 種類「デスクトップ アプリ」を選びます。
4. JSON をダウンロードし、インストールで作られたフォルダに保存して、権限を 600 にします。

```bash
mv ~/Downloads/client_secret_*.json ~/.config/meeting-guard/gcp-oauth.keys.json
chmod 600 ~/.config/meeting-guard/gcp-oauth.keys.json
```

権限が 600 でない、シンボリックリンクである、自分の持ち物でない、のどれかに当たると読み込みを拒否します。

### 3. Google の認可

```bash
/usr/bin/python3 ~/.local/share/meeting-guard/meeting_guard.py --auth
```

ブラウザで Google の同意画面が開きます（開かないときは表示された URL を開いてください）。
許可すると `~/.config/meeting-guard/tokens.json`（権限 600）が保存されます。保存するのは `refresh_token` と許可範囲（scope）だけです。
要求する権限は **`calendar.events.readonly`（予定の読み取りだけ）** です。meeting-guard はカレンダーに書き込みません。

### 4. 確認

```bash
/usr/bin/python3 ~/.local/share/meeting-guard/meeting_guard.py --check
```

認証と、24 時間以内の会議候補が表示されれば完了です。

### 5. 通しの動作確認

```bash
/usr/bin/python3 ~/.local/share/meeting-guard/meeting_guard.py --simulate
```

「meeting-guard simulation」という架空の予定で、各段階（音声・ダイアログ・プッシュ）をこの Mac の上で順に鳴らします。カレンダーには何も書かず、電話もかけません。プッシュは ntfy を設定しているときだけ送られます。

### 6. 招待の受け付け設定（おすすめ）

Google カレンダーの「設定」→「予定の設定」→「招待をカレンダーに追加」を **「知っている送信者からの招待のみ」** にしてください。知らない人からの招待がカレンダーに入らなくなり、そもそも警報の対象になりません。

### 7. スマホ通知（任意）

1. `install.sh` が表示したランダムなトピック名（または自分で作った長いランダム文字列）を、`~/.config/meeting-guard/secrets.env` の `NTFY_TOPIC=` に書きます。
2. ntfy アプリで同じトピックを購読します。
3. アクセス制限のある ntfy サーバーを使う場合は、`NTFY_TOKEN=` にアクセストークンを書くと `Authorization: Bearer` で送ります。

`ntfy_server` は `https://` だけを受け付けます（この Mac の上のサーバー `http://127.0.0.1` / `http://localhost` だけは例外）。

### 8. 電話（任意）

1. Twilio で **API キー**（`SK` で始まる）を作り、`secrets.env` に `TWILIO_ACCOUNT_SID`、`TWILIO_API_KEY_SID`、`TWILIO_API_KEY_SECRET` を書きます。API キーは個別に無効化でき、漏れたときにアカウントの認証トークンを作り直さずに済みます。API キーが無いときだけ `TWILIO_AUTH_TOKEN` を使います。
2. `TWILIO_FROM_NUMBER`（Twilio で取った番号）と `MEETING_GUARD_CALL_TO`（自分の携帯）を `+81...` のような E.164 形式で書きます。形式が違う、例の値のまま、発信元と宛先が同じ、のどれかなら発信しません。
3. 必要なら `config.json` の `call.allowed_to_prefix`（例 `+81`）で宛先の国番号を限定します。
4. `config.json` の `call.enabled` を `true` にします。
5. `--test-call` でテスト発信できます。テスト発信も通常と同じ回数制限を通ります。

Twilio 側でもおすすめの設定:

- **Voice Geographic Permissions** で、発信先を自分の国だけに絞る。
- **Usage Triggers / 利用額アラート** を設定し、想定外の利用に気づけるようにする。

---

## 設定（config.json）

| キー | 既定値 | 説明 |
|---|---|---|
| `calendars` | `["primary"]` | 見るカレンダー ID |
| `include_unaccepted` | `false` | `true` にすると、承認していない招待（未回答・仮承認）も対象にする。辞退した予定は常に対象外 |
| `ntfy_server` | `https://ntfy.sh` | ntfy サーバー。`https://` のみ（`http://127.0.0.1` / `http://localhost` は可） |
| `ntfy_topic` | `""` | ntfy トピック。通常は `secrets.env` の `NTFY_TOPIC` を使います。両方とも空ならプッシュは無効 |
| `push_include_summary` | `true` | プッシュ通知に予定名を入れる。`false` なら「会議」とだけ表示（予定名を ntfy サーバーに送らない） |
| `oauth_keys` | `~/.config/meeting-guard/gcp-oauth.keys.json` | OAuth クライアント JSON（権限 600 必須） |
| `tokens` | `~/.config/meeting-guard/tokens.json` | 認可トークン（権限 600 必須） |
| `token_profile` | `""` | トークンファイルが `{"<名前>": {"refresh_token": ...}}` の形のときの名前。`--auth` はこの名前の項目だけを書き換えます |
| `say_voice` | `""` | `say -v` に渡す声（例 `Kyoko`）。空なら既定の声 |
| `twilio_voice` | `Polly.Mizuki` | 電話の読み上げ声 |
| `twilio_language` | `ja-JP` | 電話の読み上げ言語 |
| `call_include_summary` | `false` | 電話で予定名を読み上げる。既定は予定名なしの決まり文句（予定名が Twilio の記録に残らないように） |
| `auto_open` | `true` | 開始時刻にリンクを自動で開く |
| `escalate_linkless` | `false` | リンクのない予定も開始後に繰り返し・電話をする |
| `exclude_summary_contains` | `[]` | 文字列のリスト。タイトル（無害化した後）にどれかが含まれる予定を無視（大文字・小文字は区別しません。正規表現ではなく、そのままの文字列として比べます）。例 `["昼休み", "Focus time"]` |
| `max_events_per_run` | `3` | 1 回の起動で警報を出す予定の最大数（開始が近い順） |
| `nag_minutes` | `10` | 開始後に繰り返す最大時間（分） |
| `nag_interval_sec` | `110` | 開始後の繰り返し間隔（秒） |
| `call.enabled` | `false` | 電話を使う |
| `call.after_min` | `2` | 開始何分後に電話するか |
| `call.env_file` | `~/.config/meeting-guard/secrets.env` | 秘密情報ファイル |
| `call.allowed_from_prefix` | `""` | 発信元番号をこの接頭辞（例 `+81`）に限定。空なら制限なし |
| `call.allowed_to_prefix` | `""` | 宛先番号をこの接頭辞（例 `+81`）に限定。空なら制限なし |
| `call.max_per_day` | `2` | 直近 24 時間に発信を試みてよい回数（全予定の合計） |
| `call.min_interval_min` | `30` | 前回の発信から次の発信までに空ける時間（分） |
| `stages` | `10.5 / 5.5 / 1.5 / 0.6` | 各段階の「開始何分前」。1 分間隔の起動で取りこぼさないよう 0.5 分の余裕を含みます |
| `messages` | `{}` | 読み上げ・ダイアログ・通知・電話の文言の上書き（下記） |

電話の記録は `~/.local/share/meeting-guard/calls.json` に、発信の**前に**書き込みます。このファイルが壊れている・読めない・権限が 600 でない・時刻の値がおかしい（NaN や遠い未来など）ときは、発信しない側に倒します（ログに理由が出ます）。直すか消すと再び発信できます。

**設定値の検査**: 起動のたびに、上の表のキーの型と範囲を確かめます（真偽値は `true` / `false` そのもの、数値は有限で範囲内、リストは文字列だけ。`null` も誤りとして扱います）。検査そのものが想定外の理由で失敗したときも、すべて既定値で動き、電話は無効になります。誤りがあると、そのキーだけ既定値に戻し、**電話は無効にして**、警報は既定の設定で動かし続けます。JSON として読めないときは、すべて既定値で動きます。どちらの場合も、Mac の音声とダイアログで知らせます（1 時間に 1 回まで）。主な範囲: `max_events_per_run` 1〜20（整数）、`nag_minutes` 0〜60、`nag_interval_sec` 10〜3600、`call.after_min` 0〜60、`call.max_per_day` 0〜10（整数）、`call.min_interval_min` 0〜1440、`stages` の各値 0〜60。

環境変数:

- `MEETING_GUARD_CONFIG`: config.json の場所（既定はスクリプトと同じフォルダ）
- `MEETING_GUARD_HOME`: 状態・ログ・ack・電話記録の保存先（既定 `~/.local/share/meeting-guard`）。起動のたびに、自分の持ち物であること、グループ・他人が書き込めないこと、シンボリックリンクでないことを、そこへ何か書き込む前に確かめます。満たさないときは理由を標準エラーに出し、音声とダイアログで知らせ、何も書き込まずに終了します（`--simulate` と `--test-call` も実行せず、「参加します」のクリックも記録しません）。
- `TWILIO_ACCOUNT_SID` / `TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` / `MEETING_GUARD_CALL_TO` / `NTFY_TOPIC` / `NTFY_TOKEN`: 設定すると `secrets.env` より優先されます。**空の値を設定する**（例 `NTFY_TOPIC=""`）と、その項目は無効になります（`NTFY_TOPIC=""` ならプッシュ通知を送りません）。設定していない環境変数は何も上書きしません。

### 文言の上書き

`messages` に同じキーを書くと上書きできます。使える差し込みは `{summary}`（予定名）、`{hhmm}`（開始時刻）、`{minutes}`（分）です。
キーの一覧は `mg_config.py` の `DEFAULT_MESSAGES` を見てください。英語にする例:

```json
"messages": {
  "voice5_say": "{summary} starts in {minutes} minutes.",
  "nag_say": "{summary} has started. You have not joined yet.",
  "button_ack": "I joined (stop alerts)",
  "call_say_generic": "Your meeting has started. Please join now."
},
"say_voice": "Samantha",
"twilio_voice": "Polly.Joanna",
"twilio_language": "en-US"
```

---

## 警報の止め方

- **その会議の警報を止める**: 開始後のダイアログで「参加します（警報停止）」を押す。Zoom は会議に参加すると自動で止まります。
- **開始前のダイアログ（OK ボタン）では止まりません**（意図した仕様です）。
- **一時的に全部止める**: `launchctl bootout gui/$(id -u)/io.github.meeting-guard`
- **再開**: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.meeting-guard.plist`
- **特定の定例を対象外にする**: `exclude_summary_contains` に、タイトルに含まれる言葉を書く

---

## 制限

- **Mac がスリープ中は動きません**。復帰後の最初の起動で、いちばん差し迫った段階を 1 回だけ鳴らします。会議がすでに始まっていて `nag_minutes`（既定 10 分）以内なら、会議を開き、`call.after_min` を過ぎていればその場で電話の判定もします。**開始から `nag_minutes` を過ぎて初めて気づいた予定は鳴らさず**、ログに「skipped」と 1 回だけ記録します。スマホ側のカレンダー通知も保険として残してください。
- **カレンダーを読めないとき**（ネットワーク障害、認可切れ、`tokens.json` が無い・権限が 600 でない など）は、警報が動いていないことを Mac の音声と「OK」だけのダイアログで知らせます（1 時間に 1 回まで。ntfy を設定していればプッシュも送ります）。Google が `invalid_grant`（認可の失効・取り消し）を返したときは「`--auth` を実行してください」と知らせます。
- **複数のカレンダーのうち一部だけ読めないとき**は、読めたカレンダーの予定はいつもどおり鳴らし、読めなかったカレンダーがあることを音声とダイアログで知らせます（1 時間に 1 回まで）。
- **1 つのカレンダーにつき読むのは 5 ページ（1 ページ 30 件）まで**です。読み切れなかったときはログに残し、音声とダイアログで知らせます（1 時間に 1 回まで）。
- **会議を開けなかったとき**（`open` の失敗など）は「開きました」とは言わず、手動で参加するよう音声とダイアログで知らせます。
- **Google Meet は参加を自動検知できません**（ブラウザで動くため）。Meet ではボタンを押すまで繰り返します。
- **iPhone のサイレントモードは ntfy の通知では突破できません**。主な警報は Mac の音声とダイアログです。電話は着信設定次第で鳴ります。
- Zoom の参加検知は、自分のユーザーで動く `CptHost` プロセス（会議中だけ動く）で判定しています。前の会議から残っている `CptHost` で止まらないよう、meeting-guard が会議を開いたときは、開く直前に動いていた `CptHost` を記録しておき、開いた後に起動したプロセスだけを参加とみなします（会議を開く前に自分で参加していた場合や、この記録が取れなかった場合は、「参加した」ボタンを押すまで催促が続きます）。開かなかったとき（`auto_open` が `false`、または開くのに失敗）は「開始 2 分前」以降に起動したプロセスを参加とみなします。`auto_open` が `false` で会議が続けて並んでいると、前の会議の Zoom が開始 2 分前より後に起動していた場合、次の会議に参加済みと誤って判断することがあります。Zoom の仕様変更で効かなくなる可能性があります。
- 予定の時刻にタイムゾーンの表記がないときは、予定に付いたタイムゾーン（`start.timeZone`）で解釈し、それも分からなければこの Mac の現地時刻として扱います。
- 終日の予定、辞退した予定、キャンセルされた予定、（既定では）承認していない招待は対象外です。
- 予定の開始時刻を後から動かしても、すでに鳴らした段階はやり直しません（鳴らさずに見送った予定が、これからの時刻か `nag_minutes` 以内の時刻へ移動された場合を除く）。

---

## セキュリティ上の注意

meeting-guard は、自分専用の Mac で自分のユーザーとして動くことを前提にしています。前提（脅威モデル）と、分かっている制限は [SECURITY.md](SECURITY.md) にまとめています。ここに書いた対策は、あらゆる攻撃を防ぐことを約束するものではありません。

- **秘密情報のファイル**（`secrets.env`、`tokens.json`、`gcp-oauth.keys.json`）は、自分の持ち物で権限 600、シンボリックリンクでないことを確かめてから読みます。満たさないときは読み込みを拒否し、ログに警告を出します。
- **ntfy のトピック名は実質パスワードです**。公開サーバー（ntfy.sh）では、トピック名を知っている人は誰でも通知を読めて、送ることもできます。推測されにくい長いランダム文字列にし、人に見せないでください。**通知のタイトルと本文（既定では予定名を含む）は ntfy サーバーに送られます**。予定名を送りたくないときは `push_include_summary` を `false` にしてください。
- **Twilio は有料です**。発信ごと・番号維持に料金がかかります。発信は 1 予定 1 回、1 日 `call.max_per_day` 回、間隔 `call.min_interval_min` 分に制限され、記録が読めないときは発信しません。API キーの利用、Voice Geographic Permissions の制限、利用額アラートの設定をおすすめします。
- **発信者番号**: 「不明な発信者を消音」などの設定があると、知らない番号からの電話は鳴りません。Twilio の発信元番号を連絡先に登録してください。
- ログには秘密情報（トークン、Twilio の認証情報）を書きません。ログに出る文字列は URL を `[url]`、6 桁以上の数字を `[num]` に置き換えます。電話番号は下 4 桁だけ表示します。エラーは種類と HTTP ステータスだけを記録します。
- 予定名は外から入ってくる値として扱い、制御文字・書式制御文字・`[[` `]]`（`say` の埋め込み命令）・先頭の `-` を取り除き 80 文字に切ってから、読み上げ・ダイアログ・電話に使います。
- 開くリンクは `https://zoom.us/j/<数字>`（`*.zoom.us` を含む）と `https://meet.google.com/<xxx-yyyy-zzz>` の形に完全に一致するものだけで（数字は半角 0-9 のみ）、部品から組み立て直した URL を開きます。リンクは「会議情報（conferenceData のビデオ参加）→ hangoutLink → 場所 → 説明文」の順で探し、説明文に書かれたリンクが本物の会議リンクを上書きすることはありません。
- 認証付きの通信（Google、Twilio、ntfy）はリダイレクトをたどらず、環境変数のプロキシ設定（`http_proxy` / `https_proxy` など）も使いません（認証ヘッダーをプロキシに渡さないため）。プロキシ必須のネットワークでは動きません。
- 詳しくは [SECURITY.md](SECURITY.md) を見てください。

---

## アンインストール

```bash
./uninstall.sh                # launchd から外して plist を削除（設定・秘密情報・トークンは残す）
./uninstall.sh --purge        # 削除される場所を表示するだけ（何も変えずに終了）
./uninstall.sh --purge --yes  # 保存先と ~/.config/meeting-guard も削除
```

`--purge --yes` は、`install.sh` が置いた印（`.meeting-guard-home`）のあるフォルダだけを削除します。`/`、`$HOME`、`$HOME` の外（実際の場所をたどった結果を含む）、シンボリックリンク、改行・タブなどの制御文字や `-` で始まる部分を含むパスは削除しません。削除の直前にもう一度同じ検査をします。

Google のアクセスを取り消すには、https://myaccount.google.com/connections から meeting-guard 用に作った OAuth クライアントを削除してください。

---

## 開発

```bash
python3 -m unittest discover -s tests -v
```

テストは標準ライブラリだけで動き、外部ネットワークや実際の音声・ダイアログ・launchd は使いません（`install.sh` / `uninstall.sh` のテストは一時フォルダを `HOME` にし、偽の `launchctl` / `plutil` と、呼び出しを記録する `rm` を使います）。

---

## English

**meeting-guard** is a macOS launchd agent that escalates reminders for Zoom / Google Meet meetings until you actually join. It is designed for people who easily miss meetings (for example, people with ADHD).

Escalation: push via ntfy (10 min, opt-in) → macOS voice + push (5 min) → info dialog (1 min; clicking it does **not** stop alerts) → open the meeting link + voice + push (start) → repeat every ~2 min until you click "joined" or Zoom's in-meeting process is detected (up to `nag_minutes`) → an optional Twilio phone call 2 minutes after start. Events without a meeting link only get pre-start reminders.

Quick start:

1. `./install.sh` (validates `MEETING_GUARD_HOME`: must be inside `$HOME`, not `$HOME` itself, not a symlink, and empty or previously created by meeting-guard).
2. Create a Google Cloud OAuth client of type **Desktop app**, enable the Calendar API, save the JSON as `~/.config/meeting-guard/gcp-oauth.keys.json` and `chmod 600` it. **Important: while the OAuth consent screen's publishing status is "Testing", Google expires the refresh token after 7 days and the alarms stop** (meeting-guard detects `invalid_grant` and tells you by voice and dialog to run `--auth`). Switch the publishing status to **"In production"** (an unverified app is fine for personal use), or re-run `--auth` every 7 days.
3. `/usr/bin/python3 ~/.local/share/meeting-guard/meeting_guard.py --auth` (loopback OAuth with PKCE, scope `calendar.events.readonly`; stores only the refresh token and scope, mode 0600).
4. `... meeting_guard.py --check`, then `... meeting_guard.py --simulate` (fires every stage locally for a fake event; never calls, never writes to your calendar).

Notes:

- macOS ships python 3.9 as `/usr/bin/python3` (end-of-life upstream). It works; a newer python3 is recommended via `PYTHON=/path/to/python3 ./install.sh`.
- Only events you organized/created, invitations you accepted, or events without attendees are processed (`include_unaccepted: false`). Set Google Calendar "Add invitations to my calendar" to "Only if the sender is known".
- Secret files must be regular files owned by you with mode 0600 (no symlinks), otherwise they are refused.
- Push is opt-in: set `NTFY_TOPIC` (install prints a random suggestion; it is effectively a password). `ntfy_server` must be https (http only for localhost). `NTFY_TOKEN` is sent as `Authorization: Bearer`. Titles are sent to the ntfy server unless `push_include_summary` is false.
- Phone calls are **disabled by default** (`call.enabled: false`). Caps when enabled: 1 attempt per event, 2 per 24 h (`call.max_per_day`), 30 min apart (`call.min_interval_min`).
- Phone calls: prefer a Twilio API key (`TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET`); numbers must be E.164; `call.allowed_to_prefix` restricts destinations. Every attempt is recorded in `calls.json` before the request; at most one attempt per event, `call.max_per_day` (2) per 24 h and `call.min_interval_min` (30) between calls; an unreadable ledger blocks calls; failures are never retried. The spoken text omits the title unless `call_include_summary` is true. Limit Voice Geographic Permissions to your country and set a usage trigger.
- At most `max_events_per_run` (3) events produce alerts per run, nearest first. Rescheduling an event does not reset what already fired (exception: an event that was skipped because it was first seen more than `nag_minutes` after start, and never alerted, is re-armed if its start changes and the new start is upcoming or still within `nag_minutes`, in which case it is caught up normally (opened, nag / call check; an event without a link gets the start-time voice); call / ack records are kept). A call refused only by `max_per_day` / `min_interval_min` is not marked as done and may be placed on a later run within the nag window.
- Meeting links are taken from conferenceData video entry points, then hangoutLink, then location, then description (a description cannot override the real conference link).
- Authenticated requests ignore redirects and environment proxy variables; networks that require a proxy are not supported.
- If the calendar cannot be read (network, revoked auth, missing or insecure `tokens.json`), meeting-guard says so with voice and an OK-only dialog at most once per hour (plus push if configured). Local alarms always run before (and independently of) the phone-call path. A failing calendar does not hide the others (you are told which count failed); more than 5 pages per calendar, `invalid_grant`, an invalid `config.json` (bad keys fall back to defaults and calls are disabled) and a failed `open` are also announced locally (each at most hourly).
- `exclude_summary_contains`: list of case-insensitive substrings of the sanitized title (no regular expressions). An environment variable set to an empty value (e.g. `NTFY_TOPIC=""`) disables that setting; unset variables do not override.
- `MEETING_GUARD_HOME` is checked on every run before anything is written there (owned by you, not group/world writable, not a symlink); otherwise meeting-guard writes the reason to stderr, alerts locally and exits without writing (`--simulate` / `--test-call` refuse; a "joined" click is not recorded). A config value that is `null`, the wrong type or out of range (including huge integers) falls back to its default.
- Zoom counts as joined only if its `CptHost` process started after meeting-guard opened the link (processes already running just before the open are recorded and never count), or, when it did not open it (`auto_open` off or opening failed), at or after (start - 2 min). A `dateTime` without an offset uses the event's `start.timeZone`, else local time.
- See [SECURITY.md](SECURITY.md) for the threat model and known limitations. The measures listed here do not promise protection against every attack.
- Does not run while the Mac sleeps. After waking, an event that started within `nag_minutes` is opened and the call check runs (respecting `call.after_min`); an event first seen more than `nag_minutes` after its start is skipped (logged once). Meet attendance cannot be detected automatically.
- Uninstall with `./uninstall.sh`; `--purge --yes` also removes the data and config directories (only those carrying the install marker; paths with control characters or a component starting with `-`, or resolving outside `$HOME`, are refused; each target is re-checked right before deletion). Revoke Google access at https://myaccount.google.com/connections.

Author: okazaky. License: MIT.
