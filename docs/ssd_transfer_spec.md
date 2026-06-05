# SSD転送アプリ 実装仕様書

**プロジェクト名:** `ssd-transfer`  
**対象環境:** Ubuntu 22.04+ / CLI  
**実装言語:** Python 3.10+  
**作成日:** 2026-06-05

---

## 1. 概要

外付けSSDの接続を自動検出し、指定した転送先フォルダへファイルをコピーするCLIデーモンアプリケーション。  
起動時に転送先フォルダを指定し、SSDの着脱イベントを監視し続ける常駐プロセスとして動作する。

---

## 2. システム構成

```
ssd-transfer/
├── ssd_transfer/
│   ├── __init__.py
│   ├── main.py            # エントリーポイント・CLIパース
│   ├── monitor.py         # udevデバイス監視
│   ├── transfer.py        # ファイルコピーロジック
│   ├── progress.py        # プログレスバー表示
│   └── utils.py           # ユーティリティ関数
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## 3. 起動インターフェース

### 3.1 コマンドライン引数

```bash
ssd-transfer --dest <転送先フォルダ> [OPTIONS]
```

| 引数 | 型 | 必須 | デフォルト | 説明 |
|---|---|---|---|---|
| `--dest` | path | ✅ | - | 転送先のルートフォルダ |
| `--mode` | str | - | `sequential` | 複数SSD処理モード: `sequential` / `parallel` |
| `--filter-ext` | str (複数可) | - | `*`（全て） | コピー対象拡張子 (例: `--filter-ext .jpg .mp4`) |
| `--filter-dir` | str (複数可) | - | `*`（全て） | コピー対象ディレクトリ名 (例: `--filter-dir DCIM`) |

### 3.2 起動例

```bash
# 基本起動（全ファイルをコピー）
ssd-transfer --dest /mnt/backup

# 並列処理モード
ssd-transfer --dest /mnt/backup --mode parallel

# 特定拡張子のみ
ssd-transfer --dest /mnt/backup --filter-ext .jpg .png .mp4

# 特定フォルダのみ
ssd-transfer --dest /mnt/backup --filter-dir DCIM Pictures
```

---

## 4. 機能仕様

### 4.1 デバイス監視

**実装方法:** `pyudev` ライブラリを使用してudevイベントを監視する。

**検出条件:**
- `ACTION=add` イベント
- `SUBSYSTEM=block`
- `DEVTYPE=partition`（パーティション単位でマウント検出）

**マウント確認:** `/proc/mounts` または `psutil.disk_partitions()` を使用してマウントポイントを取得する。  
マウントが完了していない場合は最大10秒間リトライ（0.5秒間隔）する。

**起動時スキャン:** 起動時点で既にマウント済みの外付けブロックデバイスも検出対象とする。`/sys/block/` + `psutil.disk_partitions()` を走査して判定する。

**外付け判定条件（以下をすべて満たすもの）:**
- `/sys/block/<dev>/removable` が `1`
- または接続バスが `usb` / `ieee1394`
- システムディスク（`/`, `/boot` など）をマウントポイントとして持たない

### 4.2 転送先フォルダ構成

接続ごとに以下の構造でフォルダを作成する:

```
<dest>/
└── 20260605_143022/          # 接続検出タイムスタンプ (YYYYMMDD_HHMMSS)
    └── <SSD_LABEL_or_UUID>/  # SSDのファイルシステムラベル（なければUUIDの先頭8文字）
        └── (ファイルツリー)
```

**例:**
```
/mnt/backup/
├── 20260605_143022/
│   └── PHOTOS_SSD/
│       ├── DCIM/
│       └── Documents/
└── 20260605_145511/
    └── dev_a1b2c3d4/
        └── ...
```

### 4.3 ファイルコピーロジック

#### コピー判定（レジューム対応）

1. 転送先に同名ファイルが**存在しない** → コピー実行
2. 転送先に同名ファイルが**存在し、サイズが一致する** → スキップ（転送済み）
3. 転送先に同名ファイルが**存在し、サイズが不一致** → 中途半端ファイルとみなして**上書き**

#### 中途半端ファイルの防止

コピーは**一時ファイル方式**で行う:

```
転送先に <filename>.tmp として書き込み
 → 書き込み完了後に <filename> にアトミックリネーム (os.rename)
 → 失敗・中断時は .tmp ファイルを削除
```

`.tmp` ファイルが残っている場合、次回接続時にクリーンアップしてから再コピーする。

#### コピー実装

`shutil.copy2()` を使用してメタデータ（タイムスタンプ）を保持する。ファイルはチャンク単位（デフォルト 1MB）で読み書きしてプログレス更新に対応する。

### 4.4 同一SSD再接続の処理（重複コピー防止）

**SSDの同一性判定:** ファイルシステムUUIDで判定する（`blkid` コマンドまたは `/dev/disk/by-uuid/` から取得）。

**転送済み判定条件:** 転送先フォルダに同一UUIDのSSDからのコピーが既に存在する場合（サブフォルダが存在し、かつ `.transfer_complete` マーカーファイルが存在する）。

**ユーザーへの確認プロンプト（対話的）:**

```
[ssd-transfer] SSD "PHOTOS_SSD" (UUID: a1b2c3d4-...) は過去に転送済みです。
  転送先: /mnt/backup/20260605_143022/PHOTOS_SSD

  どうしますか？
  [s] スキップ（何もしない）
  [c] 新規フォルダにコピー（上書きなし）
  [r] 上書きコピー（既存ファイルも再コピー）
  選択 [s/c/r]:
```

**タイムアウト:** 30秒以内に選択がなければ `[s] スキップ` を自動選択する（標準出力に通知）。

**完了マーカー:** コピー完了時に転送先フォルダ内に `.transfer_complete` ファイルを作成する。内容はJSON形式で転送メタデータを記録する:

```json
{
  "uuid": "a1b2c3d4-...",
  "label": "PHOTOS_SSD",
  "completed_at": "2026-06-05T14:30:22+09:00",
  "total_files": 1234,
  "total_bytes": 53687091200
}
```

### 4.5 複数SSD同時接続

#### sequential モード（デフォルト）

- 検出順にキューイングし、1台ずつ順番に処理する
- `queue.Queue` を使用したワーカースレッド構成

#### parallel モード

- 各SSDを独立したスレッドで並列処理する
- `threading.Thread` を使用
- プログレスバーは各SSD1行ずつ表示（後述）

### 4.6 プログレスバー表示

**ライブラリ:** `rich` を使用する（`rich.progress.Progress`）。

#### 表示形式（sequential モード）

```
[ssd-transfer] SSD検出: PHOTOS_SSD (/dev/sdb1) → /mnt/backup/20260605_143022/PHOTOS_SSD

  コピー中: Documents/2024/photo_001.jpg
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 42.3% • 22.4 GB / 53.0 GB • 85.2 MB/s • 残り 3:42
```

#### 表示形式（parallel モード）

```
  SSD-1 PHOTOS_SSD  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  55.0% • 29.2 GB / 53.0 GB • 92.1 MB/s
  SSD-2 WORK_DATA   ━━━━━━━━━━━━━━━━━━  32.1% • 8.0 GB / 25.0 GB • 45.3 MB/s
```

**表示項目:**
- 進捗バー
- パーセンテージ
- 転送済みサイズ / 全体サイズ
- 現在の転送速度（5秒移動平均）
- 推定残り時間

### 4.7 転送先容量チェック

コピー開始前にSSDの全コピー対象ファイルの合計サイズを計算し、転送先の空き容量と比較する。

```
[警告] 転送先の空き容量が不足しています。
  必要容量: 53.0 GB
  空き容量: 12.3 GB
  不足分:   40.7 GB
  転送を中断します。
```

コピー中にも500MB書き込みごとに空き容量を確認し、残り容量が転送予定サイズの10%未満になった場合に警告を表示する。容量ゼロになる前に中断し、中途半端な `.tmp` ファイルを削除する。

### 4.8 接続切断・再接続によるレジューム

**切断検出:** udevの `ACTION=remove` イベント、またはコピー中の `IOError` / `OSError` で検出する。

**切断時の動作:**
1. 現在書き込み中の `.tmp` ファイルを削除
2. 転送状態（コピー済みファイルリスト）はファイルシステムの実ファイルで管理（DBなし）
3. 警告メッセージを表示して待機状態へ移行

```
[警告] SSD "PHOTOS_SSD" が切断されました。転送を中断します。
  転送済み: 22.4 GB / 53.0 GB (42.3%)
  再接続を待機中...
```

**再接続時の動作:**
1. 同一UUID のSSDとして認識
2. 転送先フォルダを走査してコピー済みファイルを確認（サイズ一致チェック）
3. 未コピーまたはサイズ不一致のファイルのみ転送を再開

---

## 5. エラーハンドリング

| 状況 | 動作 |
|---|---|
| 転送先フォルダが存在しない | エラー終了（起動時チェック） |
| 転送先フォルダへの書き込み権限なし | エラー終了（起動時チェック） |
| SSDのマウント失敗（10秒タイムアウト） | スキップしてログ出力、次のイベントを待機 |
| 個別ファイルのコピー失敗（パーミッションエラー等） | スキップしてカウント、最後にサマリー表示 |
| 転送先容量不足 | 転送中断、`.tmp` ファイル削除 |
| SSD切断（転送中） | 転送中断、`.tmp` ファイル削除、再接続待機 |
| `Ctrl+C` による終了 | グレースフルシャットダウン（`.tmp` ファイル削除後に終了） |

---

## 6. 転送完了サマリー

コピー完了時に以下のサマリーを標準出力に表示する:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[完了] SSD "PHOTOS_SSD" → /mnt/backup/20260605_143022/PHOTOS_SSD
  転送ファイル数:   1,234 ファイル
  スキップ数:          45 ファイル（転送済み）
  失敗数:               2 ファイル（権限エラー）
  転送サイズ:      53.0 GB
  所要時間:        10分 32秒
  平均速度:        86.2 MB/s
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 7. 依存ライブラリ

```
pyudev>=0.24.1      # udevイベント監視
psutil>=5.9.0       # ディスク情報取得
rich>=13.0.0        # プログレスバー・コンソール出力
```

**システム要件:**
- `udev` / `systemd-udevd` が動作していること
- `blkid` コマンドが使用可能なこと（`util-linux` パッケージ）
- Python 3.10 以上

---

## 8. インストール

```bash
# リポジトリクローン
git clone https://github.com/yourname/ssd-transfer.git
cd ssd-transfer

# 仮想環境作成
python3 -m venv .venv
source .venv/bin/activate

# 依存インストール
pip install -r requirements.txt

# 開発インストール（コマンドとして使えるようにする）
pip install -e .
```

---

## 9. モジュール別実装ガイド

### 9.1 `main.py`

- `argparse` でCLI引数をパース
- 起動時の転送先フォルダ存在・書き込み権限チェック
- 既存マウント済みデバイスのスキャン
- `DeviceMonitor` を起動してイベントループを開始
- `signal.signal(SIGINT, ...)` でCtrl+Cをハンドル

### 9.2 `monitor.py` — `DeviceMonitor` クラス

```python
class DeviceMonitor:
    def __init__(self, dest: Path, mode: str, filters: dict): ...
    def start(self): ...           # udevモニター開始 + 既存デバイスチェック
    def stop(self): ...            # グレースフルシャットダウン
    def _on_device_added(self, device): ...   # 追加イベントハンドラ
    def _on_device_removed(self, device): ... # 削除イベントハンドラ
    def _get_mount_point(self, devpath: str) -> Optional[Path]: ...
    def _get_device_uuid(self, devpath: str) -> str: ...
    def _is_external_device(self, device) -> bool: ...
```

### 9.3 `transfer.py` — `TransferJob` クラス

```python
class TransferJob:
    def __init__(self, src: Path, dest: Path, filters: dict, uuid: str): ...
    def start(self): ...           # コピー開始
    def cancel(self): ...          # キャンセル（切断時）
    def _scan_files(self) -> List[Path]: ...          # コピー対象ファイル列挙
    def _copy_file(self, src_file: Path, dest_file: Path): ... # 1ファイルコピー（tmp方式）
    def _check_disk_space(self, required_bytes: int): ...
    def _cleanup_tmp_files(self): ...
```

### 9.4 `progress.py` — `ProgressDisplay` クラス

```python
class ProgressDisplay:
    def __init__(self, mode: str): ...
    def add_job(self, job_id: str, label: str, total_bytes: int): ...
    def update(self, job_id: str, copied_bytes: int, current_file: str): ...
    def complete(self, job_id: str, summary: dict): ...
    def error(self, job_id: str, message: str): ...
```

---

## 10. 実装上の注意点

1. **udevイベントのレースコンディション:** デバイス追加イベント直後はまだマウントされていない場合がある。マウントポイント取得はリトライ機構を設ける（最大10秒）。

2. **`blkid` の権限:** `blkid` はroot権限なしでもUUIDを取得できるが、環境によっては `sudo` が必要になる場合がある。`/dev/disk/by-uuid/` のシンボリックリンクをフォールバックとして使う。

3. **parallel モードのプログレス表示:** `rich` の `Live` コンテキストを使い複数行を同時更新する。スレッドセーフのためロックを使用する。

4. **ファイルシステム互換性:** SSDがexFAT/NTFS/FAT32の場合、一部のタイムスタンプが保持されない。`shutil.copy2()` はベストエフォートで対応する。

5. **大量ファイルのスキャン:** ファイル列挙はジェネレーターで実装し、メモリ使用量を抑える。

6. **対話プロンプトとプログレスバーの競合:** `rich` の `Live` 表示中にinputを受け付ける際は、一時的に `Live` を停止してからプロンプトを表示し、入力後に再開する。

---

## 11. 将来拡張（スコープ外）

- systemdサービスとして自動起動
- Web UI / TUIによるステータス表示
- rsync/rcloneバックエンドへの切り替えオプション
- ファイルハッシュ（MD5/SHA256）による整合性検証オプション
- 転送完了時のデスクトップ通知（`notify-send`）
