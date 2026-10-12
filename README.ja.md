# ET240 CLI

[English](README.md) | 日本語

`et240_cli.py` は POWER-Z ET240 を Windows の HID と WinUSB で直接制御するCLIです。[CY4500 CLI](https://github.com/inuchanbt/cy4500-cli) の主要コマンドとCSV形式に対応します。
**実行時に Sniffer.exe / SnifferCmd.exe / ベンダー DLL を使いません。追加の Python パッケージも不要です。**

初期バージョンでは、PDの受動収録、電圧・電流の記録、USB生データの再デコード、Analyzer Utility向け出力、PPS/AVS遷移のオフライン解析に対応します。
SN3005で直接制御を確認しています。実機でのPDネゴシエーション、EPR、負荷時の測定精度は未検証です。検証内容は [VALIDATION.md](VALIDATION.md) を参照してください。

## 必要な環境

Windows 10/11、Python 3.10 以降を使用してください。ET240 は既存の HID + WinUSB ドライバーで動きます。ドライバーの置き換えは不要です。
検証した機器はSN3005、FW `A1 05 23 11 30 E2` です。現在は他の機種エディションをエラーにします。
Sniffer.exe や他の収録ソフトは終了してから実行してください。
このリポジトリの Python ファイルは一緒に配置してください。`requirements.txt` は環境要件の説明用で、pipで追加するパッケージはありません。

## 使い方

リポジトリをクローン、またはZIPをダウンロードして展開し、そのフォルダーからPowerShellで実行します。

```powershell
git clone https://github.com/inuchanbt/ET240-CLI.git
cd ET240-CLI
python .\et240_cli.py usb-info
python .\et240_cli.py version
python .\et240_cli.py live-status --count 10
python .\et240_cli.py capture
```

`capture` は Ctrl+C で停止して保存します。既定では PD・波形・ccgx3 を保存し、正常な GoodCRC だけコンソールで非表示にします。
保存データから GoodCRC は除きません。

```powershell
# 10秒収録。出力先を明示
python .\et240_cli.py capture --seconds 10 --out-prefix .\captures\et240_test

# ccgx3を省略し、PD CSVとJSONL・生データを保存
python .\et240_cli.py capture --seconds 10 --no-ccgx3

# PDのみ保存（時刻同期と電圧取得のため、機器のテレメトリー受信は継続）
python .\et240_cli.py capture --seconds 10 --no-scope

# 波形のネイティブUSBデータも別ファイルに保存
python .\et240_cli.py capture --seconds 10 --scope-raw

# PPS/AVS応答を収録後に解析
python .\et240_cli.py capture --seconds 30 --analyze-transitions

# 波形CSVの保存先を指定。PDなどの付随ファイルは wave_session.* に保存
python .\et240_cli.py scope --seconds 5 --csv .\captures\wave.csv
```

同名の出力がある場合はエラーにします。上書きする場合だけ `--force` を指定します。
複数台の場合は `version` / `capture` / `scope` / `live-status` に `--device USBシリアル番号` を指定してください。`usb-info` で機器を一覧表示できます。
収録オプションの一覧は `python .\et240_cli.py capture --help` で確認できます。

## 再デコードとオフライン解析

以下のコマンドにはET240の接続は不要です。

```powershell
# 既存収録をハードウェアなしで再デコード
python .\et240_cli.py export-gui --xfers .\captures\et240_test.xfers.bin --out-prefix .\captures\redecoded

# 互換レコードをCSV/ccgx3に変換（PDのみ）
python .\et240_cli.py export-gui --records .\captures\et240_test.records.bin --out-prefix .\captures\converted

# 付属Snifferの.gz収録を変換（PDのみ。波形はインポートしない）
python .\et240_cli.py export-gui --trace .\traces\01_pd_bmc.gz --out-prefix .\captures\example01

# 保存済みCSVから解析
python .\et240_cli.py analyze-sync --pd-csv .\captures\et240_test.csv --scope-csv .\captures\et240_test.scope.csv --out-prefix .\captures\analysis
```

ベンダーの収録サンプルは任意のローカル入力です。このリポジトリには同梱していません。

## cy4500_cli.py との互換性

| 項目 | 対応 |
|---|---|
| `usb-info`, `version` | ET240の情報を直接取得 |
| `capture --seconds / --until-ctrl-c / --out-prefix` | 対応。既定はCtrl+Cまで |
| `--scope / --no-scope / --scope-raw` | 対応 |
| `--ccgx3 / --no-ccgx3 / --gui-csv` | 対応。ccgx3はCY4500形式への変換 |
| `--hide-goodcrc / --show-goodcrc / --quiet` | 対応 |
| `live-status`, `volt-amp` | 対応。ET240の収録セッションを使う |
| `scope --csv / --raw` | 対応 |
| `analyze-sync`, `--analyze-transitions` | 既存CY4500のPPS/AVS解析を使用 |
| `export-gui --records` | 互換レコードの変換に対応 |
| `trigger*`, `termination*` | 非対応。ET240で確認できた対応命令がないため終了コード2 |
| PythonのCY4500EPRクラスAPI | 互換対象外。ET240はet240_usb.ET240を使用 |

コマンドとデータ形式の互換性を対象にしています。USBパケット形式やコンソール全文・JSON内部構造の完全一致ではありません。
DP AUX / USB4 sideband のデコードはこの版の対象外です。収録時は無効にしています。

## 出力

既定の出力先は `captures/et240_日時.*` です。

| ファイル | 内容 |
|---|---|
| `.csv` | CY4500 Utilityと同じ15列のPD CSV |
| `.scope.csv` | CY4500解析で読める波形CSV。末尾にET240のカウンター・CC電流などを追加 |
| `.xfers.bin` | **ET240から受信した本物のUSBデータ**。uint32 LE長さ + 転送内容 |
| `.scope.xfers.bin` | `--scope-raw`時のネイティブテレメトリー転送 |
| `.records.bin` | **CY4500互換の64バイト形式に変換したレコード**。ET240の生USBデータではない |
| `.records.hex.txt` | 上記の互換レコードの16進表示 |
| `.records.jsonl` | PDペイロード・CRC・CCチャネル・生時刻・BMC間隔・互換デコード結果 |
| `.ccgx3` | Analyzer Utility形式に変換したPD/波形セッション |
| `.summary.txt`, `.metadata.json` | 件数・エラー・同期・機器情報・単位・出力一覧 |
| `.transitions.*`, `.transition_summary.*` | オプションのPPS/AVS解析結果 |

PD CSVは `Sno,Ok,SOP,Message,Msg Id,Data Role,Power Role,Obj Count,Rev,Duration,Delta,Vbus(V),Data,Start Time,End Time` の順序です。
**`Vbus(V)` 列は元のCY4500 Utility互換仕様に合わせ、整数ミリボルトです。** 波形CSVの `vbus_V` / `ibus_A` はV/Aです。

PDと波形にはET240共通の192 MHzカウンターを使用し、最初に同期できたテレメトリーを0とするマイクロ秒で保存します。
生のカウンター値はJSONLと波形CSVに残します。ホストの取得時刻で合わせる処理はありません。
PDのVBUSは直前のテレメトリーから付与します。1秒以内の先行サンプルがないときはCSVを空欄にします。

互換 `.records.bin` は時刻を整数µs・32ビットにし、電圧をCY4500のADC刻みに量子化します。
ccgx3の波形もCY4500のADC形式に量子化します。精密な数値はET240の `.scope.csv` と `.records.jsonl` を使用してください。
ccgx3はファイル構造の検証済みですが、Analyzer Utilityでの表示は未検証です。

## 検証と制限

- 接続中のET240、SN3005、FW `A1 05 23 11 30 E2` で機器情報取得・開始/停止・再接続を確認。
- 3秒の実機収録で499件の波形を保存し、USB生データから同じ波形を再現。
- 付属5収録の228 PDパケットで、ペイロード・CRC・SOPが一致。
- 自動テストで転送境界・時刻の折り返し・CRCエラー・切断時の保存・上書き防止・PPS遷移解析を確認。
- 実機確認時はVBUSがほぼ0Vで、新しいPD通信はありませんでした。実機上のPDネゴシエーション/EPRと負荷時の測定精度は未検証です。
- 長いBIST Carrier Mode 2と、互換64バイト形式に収まらない拡張パケットは完全互換の対象外です。ネイティブUSBデータは保存します。

```powershell
python -m unittest discover -s .\tests -v
```

自動テストにハードウェアは不要です。ベンダーサンプルが隣接フォルダー `../PD_Sniffer v1.08.26.0809/trace_example` にない場合、その照合テストだけスキップします。

実装した通信仕様は [PROTOCOL.md](PROTOCOL.md)、詳しい検証記録は [VALIDATION.md](VALIDATION.md) を参照してください。

## ライセンス

[MITライセンス](LICENSE) です。`ezpd_protocol.py`、`utility_export.py`、`pd_capture.py`、オフライン解析は [CY4500 CLI](https://github.com/inuchanbt/cy4500-cli) のMITライセンス実装を再利用しています。
実行時に元のリポジトリやベンダーソフトを参照する依存関係はありません。
