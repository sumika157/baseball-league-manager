"""仮想の選手データを投入する。

各チーム最大40人（実運用に近い28〜40人でばらつかせる）まで、既存の選手・在籍は
残したまま不足分を架空の選手で埋める。ポジション構成・年齢分布はMLBの40人ロース
ターの構成比をおおまかに参考にする。

一部（約12%）は「助っ人」として nationality / is_foreign_player を設定する
（既存データは 0024_backfill_foreign_players_from_birthplace で移行済み）。

氏名は「苗字＋名前」を漢字プールから組み合わせて作るため、よみがな（カタカナ）と
背ネーム（ユニフォーム背面のヘボン式アルファベット表記）もプール側に持たせてある。
背ネームは同じチーム内に同姓の選手がいる場合、ファーストネームの頭文字＋ピリオドを
先頭に付けて区別する（例: 「K.SATO」）。
"""

import random
from collections import Counter, defaultdict
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.domain.value_objects import Handedness, Position
from myapp.models import Player, PlayerStint, Team

JAPANESE_PREFECTURES = [
    '北海道', '青森県', '岩手県', '宮城県', '秋田県', '山形県', '福島県',
    '茨城県', '栃木県', '群馬県', '埼玉県', '千葉県', '東京都', '神奈川県',
    '新潟県', '富山県', '石川県', '福井県', '山梨県', '長野県', '岐阜県',
    '静岡県', '愛知県', '三重県', '滋賀県', '京都府', '大阪府', '兵庫県',
    '奈良県', '和歌山県', '鳥取県', '島根県', '岡山県', '広島県', '山口県',
    '徳島県', '香川県', '愛媛県', '高知県', '福岡県', '佐賀県', '長崎県',
    '熊本県', '大分県', '宮崎県', '鹿児島県', '沖縄県',
]

# (漢字, よみがな, ヘボン式ローマ字) の組。ローマ字は背ネームの元になる。
JP_SURNAMES = [
    ('佐藤', 'サトウ', 'SATO'), ('鈴木', 'スズキ', 'SUZUKI'), ('高橋', 'タカハシ', 'TAKAHASHI'),
    ('田中', 'タナカ', 'TANAKA'), ('伊藤', 'イトウ', 'ITO'), ('渡辺', 'ワタナベ', 'WATANABE'),
    ('山本', 'ヤマモト', 'YAMAMOTO'), ('中村', 'ナカムラ', 'NAKAMURA'), ('小林', 'コバヤシ', 'KOBAYASHI'),
    ('加藤', 'カトウ', 'KATO'), ('吉田', 'ヨシダ', 'YOSHIDA'), ('山田', 'ヤマダ', 'YAMADA'),
    ('佐々木', 'ササキ', 'SASAKI'), ('山口', 'ヤマグチ', 'YAMAGUCHI'), ('松本', 'マツモト', 'MATSUMOTO'),
    ('井上', 'イノウエ', 'INOUE'), ('木村', 'キムラ', 'KIMURA'), ('林', 'ハヤシ', 'HAYASHI'),
    ('斎藤', 'サイトウ', 'SAITO'), ('清水', 'シミズ', 'SHIMIZU'), ('山崎', 'ヤマザキ', 'YAMAZAKI'),
    ('森', 'モリ', 'MORI'), ('池田', 'イケダ', 'IKEDA'), ('橋本', 'ハシモト', 'HASHIMOTO'),
    ('阿部', 'アベ', 'ABE'), ('石川', 'イシカワ', 'ISHIKAWA'), ('前田', 'マエダ', 'MAEDA'),
    ('藤田', 'フジタ', 'FUJITA'), ('後藤', 'ゴトウ', 'GOTO'), ('岡田', 'オカダ', 'OKADA'),
    ('長谷川', 'ハセガワ', 'HASEGAWA'), ('村上', 'ムラカミ', 'MURAKAMI'), ('近藤', 'コンドウ', 'KONDO'),
    ('石井', 'イシイ', 'ISHII'), ('坂本', 'サカモト', 'SAKAMOTO'), ('遠藤', 'エンドウ', 'ENDO'),
    ('青木', 'アオキ', 'AOKI'), ('藤井', 'フジイ', 'FUJII'), ('西村', 'ニシムラ', 'NISHIMURA'),
    ('福田', 'フクダ', 'FUKUDA'), ('太田', 'オオタ', 'OTA'), ('三浦', 'ミウラ', 'MIURA'),
    ('岡本', 'オカモト', 'OKAMOTO'), ('松田', 'マツダ', 'MATSUDA'), ('中川', 'ナカガワ', 'NAKAGAWA'),
    ('中野', 'ナカノ', 'NAKANO'), ('原田', 'ハラダ', 'HARADA'), ('小野', 'オノ', 'ONO'),
    ('田村', 'タムラ', 'TAMURA'), ('竹内', 'タケウチ', 'TAKEUCHI'),
    ('新井', 'アライ', 'ARAI'), ('藤原', 'フジワラ', 'FUJIWARA'), ('三宅', 'ミヤケ', 'MIYAKE'),
    ('内田', 'ウチダ', 'UCHIDA'), ('高木', 'タカギ', 'TAKAGI'), ('安田', 'ヤスダ', 'YASUDA'),
    ('谷口', 'タニグチ', 'TANIGUCHI'), ('大野', 'オオノ', 'ONO'), ('高田', 'タカダ', 'TAKADA'),
    ('平野', 'ヒラノ', 'HIRANO'), ('西田', 'ニシダ', 'NISHIDA'), ('桑原', 'クワバラ', 'KUWABARA'),
    ('千葉', 'チバ', 'CHIBA'), ('増田', 'マスダ', 'MASUDA'), ('小川', 'オガワ', 'OGAWA'),
    ('大塚', 'オオツカ', 'OTSUKA'), ('久保', 'クボ', 'KUBO'), ('松井', 'マツイ', 'MATSUI'),
    ('野口', 'ノグチ', 'NOGUCHI'), ('菅原', 'スガワラ', 'SUGAWARA'), ('和田', 'ワダ', 'WADA'),
    ('上田', 'ウエダ', 'UEDA'), ('森田', 'モリタ', 'MORITA'), ('荒木', 'アラキ', 'ARAKI'),
    ('望月', 'モチヅキ', 'MOCHIZUKI'), ('今井', 'イマイ', 'IMAI'), ('小島', 'コジマ', 'KOJIMA'),
    ('服部', 'ハットリ', 'HATTORI'), ('大西', 'オオニシ', 'ONISHI'), ('柴田', 'シバタ', 'SHIBATA'),
    ('宮崎', 'ミヤザキ', 'MIYAZAKI'), ('杉山', 'スギヤマ', 'SUGIYAMA'), ('横山', 'ヨコヤマ', 'YOKOYAMA'),
    ('宮本', 'ミヤモト', 'MIYAMOTO'), ('内藤', 'ナイトウ', 'NAITO'), ('高山', 'タカヤマ', 'TAKAYAMA'),
    ('渡部', 'ワタベ', 'WATABE'), ('金子', 'カネコ', 'KANEKO'), ('中山', 'ナカヤマ', 'NAKAYAMA'),
    ('石田', 'イシダ', 'ISHIDA'), ('上野', 'ウエノ', 'UENO'), ('小山', 'コヤマ', 'KOYAMA'),
    ('西山', 'ニシヤマ', 'NISHIYAMA'), ('菊地', 'キクチ', 'KIKUCHI'), ('安藤', 'アンドウ', 'ANDO'),
    ('浅野', 'アサノ', 'ASANO'), ('大川', 'オオカワ', 'OKAWA'), ('北村', 'キタムラ', 'KITAMURA'),
    ('南', 'ミナミ', 'MINAMI'), ('東', 'アズマ', 'AZUMA'), ('西', 'ニシ', 'NISHI'),
    ('川村', 'カワムラ', 'KAWAMURA'), ('川崎', 'カワサキ', 'KAWASAKI'), ('川口', 'カワグチ', 'KAWAGUCHI'),
    ('飯田', 'イイダ', 'IIDA'), ('広瀬', 'ヒロセ', 'HIROSE'), ('浜田', 'ハマダ', 'HAMADA'),
    ('金田', 'カネダ', 'KANEDA'), ('石橋', 'イシバシ', 'ISHIBASHI'), ('坂井', 'サカイ', 'SAKAI'),
    ('坂口', 'サカグチ', 'SAKAGUCHI'), ('永井', 'ナガイ', 'NAGAI'), ('今村', 'イマムラ', 'IMAMURA'),
    ('本田', 'ホンダ', 'HONDA'), ('本間', 'ホンマ', 'HONMA'), ('栗原', 'クリハラ', 'KURIHARA'),
    ('栗田', 'クリタ', 'KURITA'), ('清野', 'セイノ', 'SEINO'), ('上原', 'ウエハラ', 'UEHARA'),
    ('中田', 'ナカタ', 'NAKATA'), ('中沢', 'ナカザワ', 'NAKAZAWA'), ('黒川', 'クロカワ', 'KUROKAWA'),
    ('黒木', 'クロキ', 'KUROKI'), ('黒田', 'クロダ', 'KURODA'), ('関口', 'セキグチ', 'SEKIGUCHI'),
    ('関根', 'セキネ', 'SEKINE'), ('沢田', 'サワダ', 'SAWADA'), ('岩崎', 'イワサキ', 'IWASAKI'),
    ('岩田', 'イワタ', 'IWATA'), ('岩本', 'イワモト', 'IWAMOTO'), ('星野', 'ホシノ', 'HOSHINO'),
    ('片山', 'カタヤマ', 'KATAYAMA'), ('片岡', 'カタオカ', 'KATAOKA'), ('小池', 'コイケ', 'KOIKE'),
    ('牧野', 'マキノ', 'MAKINO'), ('牧田', 'マキタ', 'MAKITA'), ('須藤', 'スドウ', 'SUDO'),
    ('須田', 'スダ', 'SUDA'), ('熊谷', 'クマガイ', 'KUMAGAI'), ('古川', 'フルカワ', 'FURUKAWA'),
    ('古谷', 'フルヤ', 'FURUYA'), ('古賀', 'コガ', 'KOGA'), ('江口', 'エグチ', 'EGUCHI'),
    ('江藤', 'エトウ', 'ETO'), ('遠山', 'トオヤマ', 'TOYAMA'), ('藤本', 'フジモト', 'FUJIMOTO'),
    ('藤川', 'フジカワ', 'FUJIKAWA'), ('藤野', 'フジノ', 'FUJINO'), ('今泉', 'イマイズミ', 'IMAIZUMI'),
    ('秋山', 'アキヤマ', 'AKIYAMA'), ('秋元', 'アキモト', 'AKIMOTO'), ('土屋', 'ツチヤ', 'TSUCHIYA'),
    ('土井', 'ドイ', 'DOI'), ('柳沢', 'ヤナギサワ', 'YANAGISAWA'), ('柳田', 'ヤナギダ', 'YANAGIDA'),
    ('梅田', 'ウメダ', 'UMEDA'), ('梅原', 'ウメハラ', 'UMEHARA'), ('桜井', 'サクライ', 'SAKURAI'),
    ('桜田', 'サクラダ', 'SAKURADA'), ('宮田', 'ミヤタ', 'MIYATA'),
]

JP_GIVEN_NAMES = [
    ('翔太', 'ショウタ', 'SHOTA'), ('大輔', 'ダイスケ', 'DAISUKE'), ('健太', 'ケンタ', 'KENTA'),
    ('直樹', 'ナオキ', 'NAOKI'), ('拓也', 'タクヤ', 'TAKUYA'), ('亮太', 'リョウタ', 'RYOTA'),
    ('陽介', 'ヨウスケ', 'YOSUKE'), ('涼介', 'リョウスケ', 'RYOSUKE'), ('康太', 'コウタ', 'KOTA'),
    ('大樹', 'ダイキ', 'DAIKI'), ('悠斗', 'ユウト', 'YUTO'), ('颯太', 'ソウタ', 'SOTA'),
    ('蓮', 'レン', 'REN'), ('大和', 'ヤマト', 'YAMATO'), ('陸', 'リク', 'RIKU'),
    ('樹', 'イツキ', 'ITSUKI'), ('陽翔', 'ハルト', 'HARUTO'), ('湊', 'ミナト', 'MINATO'),
    ('蒼', 'アオイ', 'AOI'), ('悠真', 'ユウマ', 'YUMA'), ('大翔', 'ヒロト', 'HIROTO'),
    ('匠', 'タクミ', 'TAKUMI'), ('龍之介', 'リュウノスケ', 'RYUNOSUKE'), ('秀樹', 'ヒデキ', 'HIDEKI'),
    ('誠', 'マコト', 'MAKOTO'), ('隆', 'タカシ', 'TAKASHI'), ('学', 'マナブ', 'MANABU'),
    ('剛', 'ゴウ', 'GO'), ('修', 'オサム', 'OSAMU'), ('亮', 'リョウ', 'RYO'),
    ('智也', 'トモヤ', 'TOMOYA'), ('雄大', 'ユウダイ', 'YUDAI'), ('一輝', 'カズキ', 'KAZUKI'),
    ('光', 'ヒカル', 'HIKARU'), ('健吾', 'ケンゴ', 'KENGO'), ('俊', 'シュン', 'SHUN'),
    ('慶介', 'ケイスケ', 'KEISUKE'), ('裕貴', 'ユウキ', 'YUKI'), ('春希', 'ハルキ', 'HARUKI'),
    ('光誠', 'コウセイ', 'KOSEI'), ('涼', 'リョウ', 'RYO'), ('康介', 'コウスケ', 'KOSUKE'),
    ('拓真', 'タクマ', 'TAKUMA'), ('直人', 'ナオト', 'NAOTO'), ('和也', 'カズヤ', 'KAZUYA'),
    ('昇', 'ノボル', 'NOBORU'), ('大地', 'ダイチ', 'DAICHI'), ('幹太', 'カンタ', 'KANTA'),
    ('佑樹', 'ユウキ', 'YUKI'), ('竜也', 'タツヤ', 'TATSUYA'),
    ('陽仁', 'ハルヒト', 'HARUHITO'), ('蒼真', 'ソウマ', 'SOMA'), ('楽人', 'ラクト', 'RAKUTO'),
    ('蒼空', 'ソラ', 'SORA'), ('大晴', 'タイセイ', 'TAISEI'), ('陽大', 'ハルヒロ', 'HARUHIRO'),
    ('湊翔', 'ソウト', 'SOTO'), ('蒼翔', 'ソウショウ', 'SOSHO'), ('蒼元', 'ソウゲン', 'SOGEN'),
    ('陽琉', 'ハル', 'HARU'), ('蒼吾', 'ソウゴ', 'SOGO'), ('湊心', 'ソウシン', 'SOSHIN'),
    ('悠飛', 'ユウヒ', 'YUHI'), ('光希', 'コウキ', 'KOKI'), ('蒼夢', 'ソウム', 'SOMU'),
    ('湊生', 'ソウセイ', 'SOSEI'), ('蒼樹', 'アオキ', 'AOKI'), ('陽路', 'ハルミチ', 'HARUMICHI'),
    ('蒼吏', 'ソウリ', 'SORI'), ('大翔琉', 'タイガ', 'TAIGA'), ('湊真', 'ソウマ', 'SOMA'),
    ('蒼玖', 'ソウク', 'SOKU'), ('光陽', 'ミツハル', 'MITSUHARU'), ('蒼志郎', 'ソウシロウ', 'SOSHIRO'),
    ('楓真', 'フウマ', 'FUMA'), ('楓斗', 'フウト', 'FUTO'), ('楓太', 'フウタ', 'FUTA'),
    ('蒼晴', 'ソウセイ', 'SOSEI'), ('陽晴', 'ヨウセイ', 'YOSEI'), ('蒼英', 'ソウエイ', 'SOEI'),
    ('湊聖', 'ソウセイ', 'SOSEI'), ('悠李', 'ユウリ', 'YURI'), ('蒼李', 'ソウリ', 'SORI'),
    ('陽李', 'ハルリ', 'HARURI'), ('旭', 'アサヒ', 'ASAHI'), ('悠悟', 'ユウゴ', 'YUGO'),
    ('蒼悟', 'ソウゴ', 'SOGO'), ('湊悟', 'ソウゴ', 'SOGO'), ('陽悟', 'ヨウゴ', 'YOGO'),
    ('千紘', 'チヒロ', 'CHIHIRO'), ('湊陽', 'ソウヨウ', 'SOYO'),
]

# 珍しい苗字・名前。実在するが日常ではあまり見ない組み合わせを混ぜることで、
# 全員が上位頻出の姓名にならないようにする（make_japanese_name で一定割合抽選）。
# 特定の1〜2種類（例:「一ノ瀬」ばかり）に偏ったり、大名・公家の家名（「〜小路」
# 「龍造寺」など）が時代劇めいて見えたりしないよう、現代的で由来の異なる名前を
# 広く集めてある。
RARE_JP_SURNAMES = [
    ('東海林', 'ショウジ', 'SHOJI'), ('小鳥遊', 'タカナシ', 'TAKANASHI'),
    ('四月一日', 'ワタヌキ', 'WATANUKI'), ('八月一日', 'ホズミ', 'HOZUMI'),
    ('九十九', 'ツクモ', 'TSUKUMO'), ('御手洗', 'ミタライ', 'MITARAI'),
    ('栗花落', 'ツユリ', 'TSUYURI'), ('月見里', 'ヤマナシ', 'YAMANASHI'),
    ('一尺八寸', 'カマツカ', 'KAMATSUKA'), ('六月一日', 'ウリハリ', 'URIHARI'),
    ('小文字', 'コモジ', 'KOMOJI'), ('十文字', 'ジュウモンジ', 'JUMONJI'),
    ('百目鬼', 'ドウメキ', 'DOMEKI'), ('一二三', 'ヒフミ', 'HIFUMI'),
    ('五十里', 'イカリ', 'IKARI'), ('八十島', 'ヤソジマ', 'YASOJIMA'),
    ('千種', 'チグサ', 'CHIGUSA'), ('蟹江', 'カニエ', 'KANIE'),
    ('一ノ瀬', 'イチノセ', 'ICHINOSE'), ('春夏冬', 'アキナイ', 'AKINAI'),
    ('小田垣', 'オダガキ', 'ODAGAKI'), ('五十鈴', 'イスズ', 'ISUZU'),
    ('小鳥居', 'コトリイ', 'KOTORII'), ('鴨志田', 'カモシダ', 'KAMOSHIDA'),
    ('桐生', 'キリュウ', 'KIRYU'), ('早乙女', 'サオトメ', 'SAOTOME'),
    ('逢坂', 'オウサカ', 'OSAKA'), ('桜庭', 'サクラバ', 'SAKURABA'),
    ('五百旗頭', 'イオキベ', 'IOKIBE'), ('十時', 'トトキ', 'TOTOKI'),
    ('小比類巻', 'コヒルイマキ', 'KOHIRUIMAKI'), ('御子柴', 'ミコシバ', 'MIKOSHIBA'),
    ('皆川', 'ミナガワ', 'MINAGAWA'), ('上野原', 'ウエノハラ', 'UENOHARA'),
    ('分部', 'ワケベ', 'WAKEBE'), ('埴生', 'ハニュウ', 'HANYU'),
    ('私市', 'キサイチ', 'KISAICHI'), ('麻植', 'オエ', 'OE'),
    ('各務', 'カガミ', 'KAGAMI'), ('陰山', 'カゲヤマ', 'KAGEYAMA'),
    ('皆藤', 'カイトウ', 'KAITO'), ('十河', 'ソゴウ', 'SOGO'),
    ('小鮒', 'コブナ', 'KOBUNA'), ('生越', 'オゴセ', 'OGOSE'),
    ('栢', 'カヤ', 'KAYA'), ('小豆澤', 'アズキザワ', 'AZUKIZAWA'),
    ('鳰', 'ニオ', 'NIO'), ('挾間', 'ハザマ', 'HAZAMA'),
    ('銭本', 'ゼニモト', 'ZENIMOTO'), ('御法川', 'ミノリカワ', 'MINORIKAWA'),
    ('生田目', 'ナバタメ', 'NABATAME'), ('四十物', 'アイモノ', 'AIMONO'),
    ('一寸木', 'マスギ', 'MASUGI'), ('波岡', 'ナミオカ', 'NAMIOKA'),
    ('海老原', 'エビハラ', 'EBIHARA'), ('蜂谷', 'ハチヤ', 'HACHIYA'), ('百瀬', 'モモセ', 'MOMOSE'),
    ('神門', 'ゴウド', 'GODO'), ('勅使河原', 'テシガハラ', 'TESHIGAHARA'), ('陸奥', 'ムツ', 'MUTSU'),
    ('十日市', 'トオカイチ', 'TOKAICHI'), ('五十部', 'イソベ', 'ISOBE'), ('八月朔日', 'ホズミ', 'HOZUMI'),
    ('尾関', 'オゼキ', 'OZEKI'), ('生駒', 'イコマ', 'IKOMA'), ('為近', 'タメチカ', 'TAMECHIKA'),
    ('銀林', 'ギンバヤシ', 'GINBAYASHI'), ('一柳', 'ヒトツヤナギ', 'HITOTSUYANAGI'), ('二階堂', 'ニカイドウ', 'NIKAIDO'),
    ('三枝', 'サエグサ', 'SAEGUSA'), ('四方', 'シカタ', 'SHIKATA'), ('五十嵐', 'イガラシ', 'IGARASHI'),
    ('六田', 'ロクダ', 'ROKUDA'), ('七海', 'ナナミ', 'NANAMI'), ('八木', 'ヤギ', 'YAGI'),
    ('百々', 'ドド', 'DODO'), ('万代', 'マンダイ', 'MANDAI'), ('奥田', 'オクダ', 'OKUDA'),
    ('深山', 'ミヤマ', 'MIYAMA'), ('岩清水', 'イワシミズ', 'IWASHIMIZU'), ('神谷', 'カミヤ', 'KAMIYA'),
    ('早見', 'ハヤミ', 'HAYAMI'), ('汐見', 'シオミ', 'SHIOMI'), ('荒井', 'アライ', 'ARAI'),
    ('塩谷', 'シオノヤ', 'SHIONOYA'), ('塩田', 'シオタ', 'SHIOTA'), ('芦田', 'アシダ', 'ASHIDA'),
    ('芝田', 'シバタ', 'SHIBATA'), ('柚木', 'ユズキ', 'YUZUKI'), ('檜垣', 'ヒガキ', 'HIGAKI'),
    ('粟井', 'アワイ', 'AWAI'), ('稗田', 'ヒエダ', 'HIEDA'), ('蕪木', 'カブラギ', 'KABURAGI'),
    ('梶山', 'カジヤマ', 'KAJIYAMA'), ('神代', 'ジンダイ', 'JINDAI'), ('桑折', 'コオリ', 'KOORI'),
    ('象潟', 'キサカタ', 'KISAKATA'), ('生方', 'ウブカタ', 'UBUKATA'), ('小田原', 'オダワラ', 'ODAWARA'),
    ('別所', 'ベッショ', 'BESSHO'), ('財前', 'ザイゼン', 'ZAIZEN'), ('帆足', 'ホアシ', 'HOASHI'),
    ('波多野', 'ハタノ', 'HATANO'), ('三ツ井', 'ミツイ', 'MITSUI'),
    ('雉子牟田', 'キジムタ', 'KIJIMUTA'), ('猪苗代', 'イナワシロ', 'INAWASHIRO'), ('鷹栖', 'タカス', 'TAKASU'),
    ('鷲尾', 'ワシオ', 'WASHIO'), ('雲雀', 'ヒバリ', 'HIBARI'), ('燕', 'ツバメ', 'TSUBAME'),
    ('白鳥', 'シラトリ', 'SHIRATORI'), ('黒羽', 'クロハ', 'KUROHA'), ('紅葉', 'モミジ', 'MOMIJI'),
    ('若葉', 'ワカバ', 'WAKABA'), ('早苗', 'サナエ', 'SANAE'), ('麦谷', 'ムギタニ', 'MUGITANI'),
    ('米田', 'ヨネダ', 'YONEDA'), ('粟生', 'アオ', 'AO'), ('豆塚', 'マメヅカ', 'MAMEZUKA'),
    ('胡桃沢', 'クルミザワ', 'KURUMIZAWA'), ('桃園', 'モモゾノ', 'MOMOZONO'), ('柿沼', 'カキヌマ', 'KAKINUMA'),
    ('梨本', 'ナシモト', 'NASHIMOTO'), ('桃井', 'モモイ', 'MOMOI'), ('杏', 'アンズ', 'ANZU'),
    ('二月田', 'ニガッタ', 'NIGATTA'), ('五月女', 'サオトメ', 'SAOTOME'), ('七五三掛', 'シメカケ', 'SHIMEKAKE'),
    ('八重尾', 'ヤエオ', 'YAEO'), ('追分', 'オイワケ', 'OIWAKE'), ('上口', 'カミグチ', 'KAMIGUCHI'),
    ('下村', 'シモムラ', 'SHIMOMURA'), ('中津川', 'ナカツガワ', 'NAKATSUGAWA'), ('遠矢', 'トオヤ', 'TOYA'),
    ('近江', 'オウミ', 'OMI'), ('深浦', 'フカウラ', 'FUKAURA'), ('浅井', 'アサイ', 'ASAI'),
    ('深沢', 'フカザワ', 'FUKAZAWA'), ('谷津', 'ヤツ', 'YATSU'), ('谷地', 'ヤチ', 'YACHI'),
    ('沼田', 'ヌマタ', 'NUMATA'), ('沼尻', 'ヌマジリ', 'NUMAJIRI'), ('池尻', 'イケジリ', 'IKEJIRI'),
    ('池上', 'イケガミ', 'IKEGAMI'), ('泉谷', 'イズミヤ', 'IZUMIYA'), ('湯浅', 'ユアサ', 'YUASA'),
    ('湯本', 'ユモト', 'YUMOTO'), ('温井', 'ヌクイ', 'NUKUI'), ('寒川', 'サムカワ', 'SAMUKAWA'),
    ('風間', 'カザマ', 'KAZAMA'), ('雷', 'イカズチ', 'IKAZUCHI'), ('雲井', 'クモイ', 'KUMOI'),
    ('霧島', 'キリシマ', 'KIRISHIMA'), ('霞', 'カスミ', 'KASUMI'), ('露木', 'ツユキ', 'TSUYUKI'),
    ('氷室', 'ヒムロ', 'HIMURO'), ('雪村', 'ユキムラ', 'YUKIMURA'), ('霜田', 'シモダ', 'SHIMODA'),
    ('星合', 'ホシアイ', 'HOSHIAI'), ('月岡', 'ツキオカ', 'TSUKIOKA'), ('日置', 'ヘキ', 'HEKI'),
    ('日下部', 'クサカベ', 'KUSAKABE'), ('山内', 'ヤマウチ', 'YAMAUCHI'), ('山下', 'ヤマシタ', 'YAMASHITA'),
    ('谷川', 'タニガワ', 'TANIGAWA'), ('岡崎', 'オカザキ', 'OKAZAKI'), ('丘', 'オカ', 'OKA'),
    ('島崎', 'シマザキ', 'SHIMAZAKI'), ('島田', 'シマダ', 'SHIMADA'), ('浦野', 'ウラノ', 'URANO'),
    ('浦島', 'ウラシマ', 'URASHIMA'), ('磯部', 'イソベ', 'ISOBE'), ('磯野', 'イソノ', 'ISONO'),
    ('波々伯部', 'ホウカベ', 'HOKABE'), ('五十棲', 'イソズミ', 'ISOZUMI'), ('錦', 'ニシキ', 'NISHIKI'),
    ('蒔田', 'マキタ', 'MAKITA'), ('種市', 'タネイチ', 'TANEICHI'), ('苗代', 'ナワシロ', 'NAWASHIRO'),
    ('稲村', 'イナムラ', 'INAMURA'), ('稲垣', 'イナガキ', 'INAGAKI'), ('麦生', 'ムギオ', 'MUGIO'),
    ('粟飯原', 'アイハラ', 'AIHARA'), ('大豆生田', 'オオマミュウダ', 'OMAMYUDA'), ('一口', 'イモアライ', 'IMOARAI'),
    ('月形', 'ツキガタ', 'TSUKIGATA'), ('星川', 'ホシカワ', 'HOSHIKAWA'), ('空閑', 'クガ', 'KUGA'),
    ('風戸', 'カザト', 'KAZATO'), ('水流', 'ツル', 'TSURU'), ('木野', 'キノ', 'KINO'),
    ('木本', 'キモト', 'KIMOTO'), ('森安', 'モリヤス', 'MORIYASU'), ('森重', 'モリシゲ', 'MORISHIGE'),
    ('森光', 'モリミツ', 'MORIMITSU'), ('岩間', 'イワマ', 'IWAMA'), ('岩永', 'イワナガ', 'IWANAGA'),
    ('石動', 'イスルギ', 'ISURUGI'), ('石飛', 'イシトビ', 'ISHITOBI'), ('金城', 'キンジョウ', 'KINJO'),
    ('玉木', 'タマキ', 'TAMAKI'), ('宝田', 'タカラダ', 'TAKARADA'), ('財津', 'ザイツ', 'ZAITSU'),
    ('塩見', 'シオミ', 'SHIOMI'),
]

RARE_JP_GIVEN_NAMES = [
    ('一心', 'イッシン', 'ISSHIN'), ('冬馬', 'トウマ', 'TOMA'), ('天馬', 'テンマ', 'TENMA'),
    ('銀河', 'ギンガ', 'GINGA'), ('颯真', 'ソウマ', 'SOMA'), ('蒼耶', 'ソウヤ', 'SOYA'),
    ('悠玄', 'ユウゲン', 'YUGEN'), ('一颯', 'イッサ', 'ISSA'), ('剛毅', 'ゴウキ', 'GOKI'),
    ('郁人', 'イクト', 'IKUTO'), ('悠聖', 'ユウセイ', 'YUSEI'), ('蒼士', 'ソウシ', 'SOSHI'),
    ('眞人', 'マサト', 'MASATO'), ('匠海', 'タクミ', 'TAKUMI'), ('大知', 'ダイチ', 'DAICHI'),
    ('澄人', 'スミト', 'SUMITO'), ('遙人', 'ハルト', 'HARUTO'), ('奏太', 'ソウタ', 'SOTA'),
    ('律', 'リツ', 'RITSU'), ('礼真', 'レイマ', 'REIMA'), ('弦', 'ゲン', 'GEN'),
    ('蒼来', 'ソラ', 'SORA'), ('悠世', 'ユウセイ', 'YUSEI'), ('泰知', 'ヤスト', 'YASUTO'),
    ('恭平', 'キョウヘイ', 'KYOHEI'), ('陽向', 'ヒナタ', 'HINATA'), ('蒼天', 'ソウタ', 'SOTA'),
    ('大空', 'オオゾラ', 'OZORA'), ('澄海', 'スミ', 'SUMI'), ('光琉', 'ヒカル', 'HIKARU'),
    ('蒼介', 'ソウスケ', 'SOSUKE'), ('陸斗', 'リクト', 'RIKUTO'), ('湊斗', 'ミナト', 'MINATO'),
    ('悠磨', 'ユウマ', 'YUMA'), ('蒼太朗', 'ソウタロウ', 'SOTARO'), ('陽和', 'ハルカズ', 'HARUKAZU'),
    ('楓雅', 'フウガ', 'FUGA'), ('律樹', 'リツキ', 'RITSUKI'), ('悠陽', 'ハルヒ', 'HARUHI'),
    ('蒼一朗', 'ソウイチロウ', 'SOICHIRO'), ('大河', 'タイガ', 'TAIGA'), ('颯斗', 'ハヤト', 'HAYATO'),
    ('蒼汰', 'ソウタ', 'SOTA'), ('湊大', 'ソウダイ', 'SODAI'),
    ('智貴', 'トモタカ', 'TOMOTAKA'), ('和斗', 'カズト', 'KAZUTO'), ('幸太', 'コウタ', 'KOTA'),
    ('幸輝', 'コウキ', 'KOKI'), ('岳', 'ガク', 'GAKU'), ('岳斗', 'ガクト', 'GAKUTO'),
    ('律仁', 'リヒト', 'RIHITO'), ('玲央', 'レオ', 'REO'), ('大成', 'タイセイ', 'TAISEI'),
    ('太一', 'タイチ', 'TAICHI'), ('圭吾', 'ケイゴ', 'KEIGO'), ('圭太', 'ケイタ', 'KEITA'),
    ('慧', 'サトシ', 'SATOSHI'), ('聡太', 'ソウタ', 'SOTA'), ('聡一郎', 'ソウイチロウ', 'SOICHIRO'),
    ('尚志', 'ナオシ', 'NAOSHI'), ('尚輝', 'ナオキ', 'NAOKI'), ('元気', 'ゲンキ', 'GENKI'),
    ('元太', 'ゲンタ', 'GENTA'), ('快斗', 'カイト', 'KAITO'), ('快晴', 'カイセイ', 'KAISEI'),
    ('悠', 'ユウ', 'YU'), ('尊', 'タケル', 'TAKERU'), ('猛', 'タケシ', 'TAKESHI'),
    ('豪', 'ゴウ', 'GO'), ('豪太', 'ゴウタ', 'GOTA'), ('力也', 'リキヤ', 'RIKIYA'),
    ('力斗', 'リキト', 'RIKITO'), ('走', 'ソウ', 'SO'), ('疾風', 'ハヤテ', 'HAYATE'),
    ('迅', 'ジン', 'JIN'), ('迅人', 'ジント', 'JINTO'), ('洸', 'コウ', 'KO'),
    ('洸太', 'コウタ', 'KOTA'), ('澪', 'レイ', 'REI'), ('漣', 'レン', 'REN'),
    ('響', 'ヒビキ', 'HIBIKI'), ('響也', 'キョウヤ', 'KYOYA'), ('颯', 'ハヤテ', 'HAYATE'),
    ('颯人', 'ハヤト', 'HAYATO'), ('惺', 'セイ', 'SEI'), ('蓮太朗', 'レンタロウ', 'RENTARO'),
    ('蓮斗', 'レント', 'RENTO'), ('蓮真', 'レンマ', 'RENMA'),
]

# 珍しい苗字・名前を抽選する確率。プール自体は104種・88種と大きいが、
# 比率を高くすると「六月一日」のような超レア姓が「佐藤」など上位頻出姓と
# 大差ない頻度で現れてしまう（プールが大きいほど1件あたりの分母が薄まるため）。
# 0.10なら「姓名とも定番」が約81%、「どちらかが珍しい」が約18%、
# 「姓名とも珍しい」が約1%になる目安で、あくまで少数の彩りにとどめる。
RARE_NAME_RATIO = 0.10

# このコマンドの初回投入より前から存在した選手。--rename-existing で名前を
# 選び直す対象から除外し、--assign-readings では固定のよみがな・ローマ字を使う
# （架空データではなく実際に登録された選手であり、生成プールに読みを持たないため）。
ORIGINAL_PLAYER_READINGS = {
    '藤井健吾': ('フジイケンゴ', 'FUJII', 'KENGO'),
    '中島亮太': ('ナカジマリョウタ', 'NAKAJIMA', 'RYOTA'),
    '渡辺春希': ('ワタナベハルキ', 'WATANABE', 'HARUKI'),
    '大田光誠': ('オオタコウセイ', 'OTA', 'KOSEI'),
    '山中俊': ('ヤマナカシュン', 'YAMANAKA', 'SHUN'),
    '坂上康太': ('サカウエコウタ', 'SAKAUE', 'KOTA'),
    '黒田裕貴': ('クロダユウキ', 'KURODA', 'YUKI'),
    '高橋誠司': ('タカハシセイジ', 'TAKAHASHI', 'SEIJI'),
    '大岩蓮': ('オオイワレン', 'OIWA', 'REN'),
    '遠藤慶介': ('エンドウケイスケ', 'ENDO', 'KEISUKE'),
    '佐藤涼介': ('サトウリョウスケ', 'SATO', 'RYOSUKE'),
}
ORIGINAL_PLAYER_NAMES = list(ORIGINAL_PLAYER_READINGS)

# 出身地は「都道府県 + 汎用の種別」の組み合わせで、実在の学校名と混同しない
# 架空の名前にする。
HIGH_SCHOOL_SUFFIXES = ['高校', '第一高校', '工業高校', '商業高校', '学園高校']
UNIVERSITIES = [
    '東湾大学', '中央国際大学', '北陵大学', '西海学院大学', '南央大学',
    '国際経済大学', '青嵐大学', '緑丘大学', '白鷺大学', '甲南学芸大学',
]
CORPORATE_TEAMS = [
    '東海重工', '北陸電機', '大和製鋼', '扶桑紡績', '中京運輸',
    '山陽化学', '関東製粉', '九州電子工業', '北都銀行', '太平洋物産',
]

# (カタカナ, ローマ字) の組。カタカナが選手名・よみがなに、ローマ字が背ネームになる。
FOREIGN_GROUPS = [
    {
        'country': 'アメリカ合衆国',
        'given': [
            ('マイケル', 'MICHAEL'), ('ジェームズ', 'JAMES'), ('ジョン', 'JOHN'),
            ('ロバート', 'ROBERT'), ('デイビッド', 'DAVID'), ('クリス', 'CHRIS'),
            ('ジャスティン', 'JUSTIN'), ('ケビン', 'KEVIN'), ('ブランドン', 'BRANDON'),
            ('タイラー', 'TYLER'),
        ],
        'surname': [
            ('ジョンソン', 'JOHNSON'), ('ウィリアムズ', 'WILLIAMS'), ('ブラウン', 'BROWN'),
            ('ミラー', 'MILLER'), ('デイビス', 'DAVIS'), ('ウィルソン', 'WILSON'),
            ('アンダーソン', 'ANDERSON'), ('トンプソン', 'THOMPSON'), ('マルティネス', 'MARTINEZ'),
            ('ロビンソン', 'ROBINSON'),
        ],
    },
    {
        'country': 'ドミニカ共和国',
        'given': [
            ('ホセ', 'JOSE'), ('フアン', 'JUAN'), ('ペドロ', 'PEDRO'),
            ('ラファエル', 'RAFAEL'), ('ミゲル', 'MIGUEL'), ('ルイス', 'LUIS'),
            ('カルロス', 'CARLOS'), ('ラモン', 'RAMON'), ('ワンダー', 'WANDER'),
            ('エルビス', 'ELVIS'),
        ],
        'surname': [
            ('マルティネス', 'MARTINEZ'), ('ロドリゲス', 'RODRIGUEZ'), ('ペレス', 'PEREZ'),
            ('サンチェス', 'SANCHEZ'), ('ラミレス', 'RAMIREZ'), ('クルーズ', 'CRUZ'),
            ('レイエス', 'REYES'), ('ペーニャ', 'PENA'), ('ゴメス', 'GOMEZ'),
            ('トーレス', 'TORRES'),
        ],
    },
    {
        'country': 'ベネズエラ',
        'given': [
            ('アレハンドロ', 'ALEJANDRO'), ('エドゥアルド', 'EDUARDO'), ('ガブリエル', 'GABRIEL'),
            ('ロナルド', 'RONALD'), ('サルバドール', 'SALVADOR'), ('アンヘル', 'ANGEL'),
            ('ウィルソン', 'WILSON'), ('フレディ', 'FREDDY'), ('ヨンダー', 'YONDER'),
            ('オジー', 'OZZIE'),
        ],
        'surname': [
            ('ゴンザレス', 'GONZALEZ'), ('エルナンデス', 'HERNANDEZ'), ('グズマン', 'GUZMAN'),
            ('マルカーノ', 'MARCANO'), ('ロンドン', 'RONDON'), ('サラザール', 'SALAZAR'),
            ('アリアス', 'ARIAS'), ('ベジョ', 'BELLO'), ('サンブラーノ', 'ZAMBRANO'),
            ('アポンテ', 'APONTE'),
        ],
    },
    {
        'country': 'キューバ',
        'given': [
            ('ヤシエル', 'YASIEL'), ('ヨルダン', 'YORDAN'), ('アロルディス', 'AROLDIS'),
            ('ユリ', 'YULI'), ('アレクセイ', 'ALEXEI'), ('エリスベル', 'ERISBEL'),
            ('フレデリク', 'FREDERICH'), ('ロエル', 'ROEL'), ('ルルデス', 'LOURDES'),
            ('ユニオル', 'YUNIOR'),
        ],
        'surname': [
            ('セペダ', 'CEPEDA'), ('グリエル', 'GURRIEL'), ('デスパイネ', 'DESPAIGNE'),
            ('アブレイユ', 'ABREU'), ('ラモス', 'RAMOS'), ('イグレシアス', 'IGLESIAS'),
            ('スアレス', 'SUAREZ'), ('プイグ', 'PUIG'), ('セスペデス', 'CESPEDES'),
            ('バルデス', 'VALDES'),
        ],
    },
    {
        'country': '大韓民国',
        'given': [
            ('ミンジュン', 'MINJUN'), ('ジフン', 'JIHOON'), ('スンヒョン', 'SEUNGHYUN'),
            ('ドンウォン', 'DONGWON'), ('ジェヨン', 'JAEYOUNG'), ('ヒョヌ', 'HYUNWOO'),
            ('ソンミン', 'SUNGMIN'), ('テヤン', 'TAEYANG'), ('キョンミン', 'KYUNGMIN'),
            ('ジュノ', 'JUNHO'),
        ],
        'surname': [
            ('キム', 'KIM'), ('イ', 'LEE'), ('パク', 'PARK'), ('チェ', 'CHOI'),
            ('チョン', 'JUNG'), ('カン', 'KANG'), ('ユン', 'YOON'), ('ハン', 'HAN'),
            ('オ', 'OH'), ('ソ', 'SEO'),
        ],
    },
    {
        'country': '台湾',
        'given': [
            ('チーウェイ', 'CHIHWEI'), ('チュンシェン', 'CHUNHSIEN'), ('ウェイイン', 'WEIYIN'),
            ('ジエミン', 'CHIEHMING'), ('ユーチェン', 'YUCHENG'), ('ポージュー', 'POJU'),
            ('カイウェイ', 'KAIWEI'), ('チェンユー', 'CHENGYU'), ('モンシュエン', 'MENGHSUAN'),
            ('イーチェン', 'YICHEN'),
        ],
        'surname': [
            ('チェン', 'CHEN'), ('リン', 'LIN'), ('ワン', 'WANG'), ('チャン', 'CHANG'),
            ('ウー', 'WU'), ('ホアン', 'HUANG'), ('シュー', 'HSU'), ('クオ', 'KUO'),
            ('ツァイ', 'TSAI'), ('ヤン', 'YANG'),
        ],
    },
    {
        'country': 'オーストラリア',
        'given': [
            ('リアム', 'LIAM'), ('ジャック', 'JACK'), ('ライアン', 'RYAN'),
            ('ネイサン', 'NATHAN'), ('トッド', 'TODD'), ('グラント', 'GRANT'),
            ('デイミアン', 'DAMIEN'), ('トレント', 'TRENT'), ('シェーン', 'SHANE'),
            ('アダム', 'ADAM'),
        ],
        'surname': [
            ('スミス', 'SMITH'), ('テイラー', 'TAYLOR'), ('ホワイト', 'WHITE'),
            ('クラーク', 'CLARKE'), ('ヒューズ', 'HUGHES'), ('ターナー', 'TURNER'),
            ('ワトソン', 'WATSON'), ('ベル', 'BELL'), ('マーシュ', 'MARSH'),
            ('クーパー', 'COOPER'),
        ],
    },
]

# MLBの40人ロースター構成をおおまかに参考にした比率。合計は1.0。
POSITION_RATIOS = {
    Position.PITCHER.value: 0.475,
    Position.CATCHER.value: 0.0625,
    Position.INFIELDER.value: 0.2375,
    Position.OUTFIELDER.value: 0.1875,
    Position.DESIGNATED_HITTER.value: 0.025,
}

# ポジションごとの体格レンジ（cm, kg）。MLB選手の体格傾向を参考にした目安。
PHYSIQUE_RANGES = {
    Position.PITCHER.value: ((178, 196), (78, 98)),
    Position.CATCHER.value: ((172, 185), (75, 92)),
    Position.INFIELDER.value: ((170, 186), (68, 88)),
    Position.OUTFIELDER.value: ((175, 190), (72, 92)),
    Position.DESIGNATED_HITTER.value: ((178, 193), (82, 100)),
}

FOREIGN_PLAYER_RATIO = 0.12
MIN_ROSTER, MAX_ROSTER = 28, 40


def largest_remainder(total, ratios):
    """比率(dict)にしたがって total を整数配分する（最大剰余法）。"""
    raw = {key: total * ratio for key, ratio in ratios.items()}
    floored = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(floored.values())
    order = sorted(raw, key=lambda key: raw[key] - floored[key], reverse=True)
    for key in order[:remainder]:
        floored[key] += 1
    return floored


def _pick_surname():
    pool = RARE_JP_SURNAMES if random.random() < RARE_NAME_RATIO else JP_SURNAMES
    return random.choice(pool)


def _pick_given_name():
    pool = RARE_JP_GIVEN_NAMES if random.random() < RARE_NAME_RATIO else JP_GIVEN_NAMES
    return random.choice(pool)


def make_japanese_name(used_names):
    """(氏名, よみがな, 苗字ローマ字, 名前ローマ字) を返す。"""
    for _ in range(50):
        surname_kanji, surname_kana, surname_romaji = _pick_surname()
        given_kanji, given_kana, given_romaji = _pick_given_name()
        name = surname_kanji + given_kanji
        if name not in used_names:
            used_names.add(name)
            return name, surname_kana + given_kana, surname_romaji, given_romaji
    # 50回試して衝突する確率は極めて低いが、念のため連番で確定させる
    name = f"{surname_kanji}{given_kanji}{len(used_names)}"
    used_names.add(name)
    return name, surname_kana + given_kana, surname_romaji, given_romaji


def make_foreign_name(used_names):
    """(氏名, よみがな, 国名, 苗字ローマ字, 名前ローマ字) を返す。"""
    group = random.choice(FOREIGN_GROUPS)
    for _ in range(50):
        given_kana, given_romaji = random.choice(group['given'])
        surname_kana, surname_romaji = random.choice(group['surname'])
        name = f"{given_kana}・{surname_kana}"
        if name not in used_names:
            used_names.add(name)
            return name, name, group['country'], surname_romaji, given_romaji
    name = f"{name}{len(used_names)}"
    used_names.add(name)
    return name, name, group['country'], surname_romaji, given_romaji


def make_birth_date(today, age):
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    had_birthday_this_year = (month, day) <= (today.month, today.day)
    birth_year = today.year - age if had_birthday_this_year else today.year - age - 1
    return date(birth_year, month, day)


def make_amateur_career(is_foreign):
    """(high_school, university, corporate_team, debut_age) を返す。"""
    if is_foreign:
        return '', '', '', random.randint(19, 30)

    path = random.choices(['high_school', 'university', 'corporate'], weights=[30, 50, 20])[0]
    high_school = f"{random.choice(JAPANESE_PREFECTURES)}{random.choice(HIGH_SCHOOL_SUFFIXES)}"
    if path == 'high_school':
        return high_school, '', '', 18
    if path == 'university':
        return high_school, random.choice(UNIVERSITIES), '', 22
    return high_school, '', random.choice(CORPORATE_TEAMS), random.randint(23, 26)


class Command(BaseCommand):
    help = '各チームに仮想の選手データを投入する（既存の選手・在籍は残す）'

    def add_arguments(self, parser):
        parser.add_argument('--seed', type=int, default=None, help='乱数シード（再現用）')
        parser.add_argument('--dry-run', action='store_true', help='投入せず件数だけ表示する')
        parser.add_argument(
            '--rename-existing', action='store_true',
            help=(
                'このコマンドで過去に投入した架空の日本人選手（助っ人を除く）の'
                '氏名を、珍しい苗字・名前を混ぜたプールで選び直す。新規投入はしない。'
            ),
        )
        parser.add_argument(
            '--assign-readings', action='store_true',
            help=(
                '全選手（実在・架空とも）によみがなと背ネームを付与する。'
                '氏名や新規投入はしない。'
            ),
        )

    def handle(self, *args, **options):
        if options['seed'] is not None:
            random.seed(options['seed'])

        if options['rename_existing']:
            self._rename_existing(dry_run=options['dry_run'])
            return

        if options['assign_readings']:
            self._assign_readings(dry_run=options['dry_run'])
            return

        today = date.today()
        used_names = set(Player.objects.values_list('name', flat=True))

        created_players = 0
        created_stints = 0
        foreign_count = 0
        team_reports = []

        with transaction.atomic():
            for team in Team.objects.all():
                active_stints = list(
                    PlayerStint.objects.filter(team=team, to_year__isnull=True)
                    .select_related('player')
                )
                existing_count = len(active_stints)
                used_numbers = {s.number for s in active_stints}
                existing_by_position = {}
                for s in active_stints:
                    existing_by_position[s.player.position] = (
                        existing_by_position.get(s.player.position, 0) + 1
                    )

                roster_size = random.randint(MIN_ROSTER, MAX_ROSTER)
                if existing_count >= roster_size:
                    team_reports.append((team.name, existing_count, 0))
                    continue

                to_add = roster_size - existing_count
                target_by_position = largest_remainder(roster_size, POSITION_RATIOS)
                add_by_position = {
                    position: max(0, target_by_position[position] - existing_by_position.get(position, 0))
                    for position in POSITION_RATIOS
                }
                # 端数調整で合計が to_add からずれる場合は内野手で吸収する
                diff = to_add - sum(add_by_position.values())
                add_by_position[Position.INFIELDER.value] += diff

                available_numbers = [n for n in range(1, 100) if n not in used_numbers]
                random.shuffle(available_numbers)

                new_players = []
                added_this_team = 0
                for position, count in add_by_position.items():
                    height_range, weight_range = PHYSIQUE_RANGES[position]
                    for _ in range(count):
                        if added_this_team >= to_add:
                            break

                        is_foreign = random.random() < FOREIGN_PLAYER_RATIO
                        if is_foreign:
                            name, name_kana, country, surname_romaji, given_romaji = (
                                make_foreign_name(used_names)
                            )
                            birthplace = country
                        else:
                            name, name_kana, surname_romaji, given_romaji = (
                                make_japanese_name(used_names)
                            )
                            birthplace = random.choice(JAPANESE_PREFECTURES)

                        age = max(19, min(40, round(random.gauss(27.5, 3.8))))
                        birth_date = make_birth_date(today, age)
                        high_school, university, corporate_team, debut_age = make_amateur_career(is_foreign)
                        debut_age = min(debut_age, age)
                        debut_year = birth_date.year + debut_age

                        if position == Position.CATCHER.value:
                            throws = Handedness.RIGHT.value if random.random() < 0.92 else Handedness.LEFT.value
                        else:
                            throws = Handedness.RIGHT.value if random.random() < 0.75 else Handedness.LEFT.value
                        bats = random.choices(
                            [Handedness.RIGHT.value, Handedness.LEFT.value, Handedness.BOTH.value],
                            weights=[55, 30, 15],
                        )[0]

                        if not available_numbers:
                            available_numbers = [n for n in range(100, 1000) if n not in used_numbers]
                            random.shuffle(available_numbers)
                        number = available_numbers.pop()
                        used_numbers.add(number)

                        if options['dry_run']:
                            created_players += 1
                            created_stints += 1
                            added_this_team += 1
                            if is_foreign:
                                foreign_count += 1
                            continue

                        player = Player.objects.create(
                            name=name,
                            name_kana=name_kana,
                            position=position,
                            birth_date=birth_date,
                            throws=throws,
                            bats=bats,
                            height_cm=random.randint(*height_range),
                            weight_kg=random.randint(*weight_range),
                            birthplace=birthplace,
                            debut_year=debut_year,
                            high_school=high_school,
                            university=university,
                            corporate_team=corporate_team,
                            nationality=birthplace if is_foreign else '',
                            is_foreign_player=is_foreign,
                        )
                        PlayerStint.objects.create(
                            player=player,
                            team=team,
                            number=number,
                            from_year=debut_year,
                            to_year=None,
                        )
                        new_players.append((player, surname_romaji, given_romaji))
                        created_players += 1
                        created_stints += 1
                        added_this_team += 1
                        if is_foreign:
                            foreign_count += 1

                if not options['dry_run']:
                    self._apply_back_names(team, new_players)

                team_reports.append((team.name, existing_count, added_this_team))

            if options['dry_run']:
                transaction.set_rollback(True)

        for name, existing, added in team_reports:
            self.stdout.write(f"{name}: 既存{existing}人 + 新規{added}人 = {existing + added}人")

        prefix = '[dry-run] ' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}選手 {created_players}人 / 在籍 {created_stints}件 を作成しました"
            f"（うち海外出身 {foreign_count}人）"
        ))

    def _apply_back_names(self, team, new_players):
        """新規追加分を、既存の在籍者も含めたチーム内の同姓関係で背ネーム付けする。"""
        if not new_players:
            return

        existing_players = [
            s.player for s in
            PlayerStint.objects.filter(team=team, to_year__isnull=True)
            .select_related('player')
            .exclude(player_id__in=[p.id for p, _, _ in new_players])
        ]

        # 既存選手の苗字ローマ字は、保存済みの背ネームから復元する
        # （末尾側が苗字。ピリオドが付いていれば取り除く）。
        surname_romaji_by_id = {}
        given_initial_by_id = {}
        for p in existing_players:
            base = p.back_name.split('.')[-1] if p.back_name else ''
            surname_romaji_by_id[p.id] = base
            given_initial_by_id[p.id] = p.back_name[0] if '.' in (p.back_name or '') else ''
        for player, surname_romaji, given_romaji in new_players:
            surname_romaji_by_id[player.id] = surname_romaji
            given_initial_by_id[player.id] = given_romaji[0] if given_romaji else ''

        counts = Counter(surname_romaji_by_id.values())

        to_update = []
        for player, _, given_romaji in new_players:
            surname_romaji = surname_romaji_by_id[player.id]
            if counts[surname_romaji] > 1:
                player.back_name = f"{given_initial_by_id[player.id]}.{surname_romaji}"
            else:
                player.back_name = surname_romaji
            to_update.append(player)
        Player.objects.bulk_update(to_update, ['back_name'])

        # 同姓が新たに発生した既存選手側の背ネームも、頭文字付きに揃え直す
        stale = [
            p for p in existing_players
            if counts[surname_romaji_by_id[p.id]] > 1 and '.' not in (p.back_name or '')
        ]
        for p in stale:
            p.back_name = f"{given_initial_by_id[p.id]}.{surname_romaji_by_id[p.id]}"
        if stale:
            Player.objects.bulk_update(stale, ['back_name'])

    def _rename_existing(self, *, dry_run):
        targets = (
            Player.objects.filter(is_foreign_player=False)
            .exclude(name__in=ORIGINAL_PLAYER_NAMES)
        )
        used_names = set(Player.objects.values_list('name', flat=True))
        renamed = 0

        with transaction.atomic():
            for player in targets:
                used_names.discard(player.name)
                new_name, name_kana, surname_romaji, given_romaji = make_japanese_name(used_names)
                if not dry_run:
                    player.name = new_name
                    player.name_kana = name_kana
                    player.save(update_fields=['name', 'name_kana'])
                renamed += 1

            if dry_run:
                transaction.set_rollback(True)

        prefix = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f"{prefix}選手 {renamed}人の氏名を選び直しました"))
        if not dry_run:
            self.stdout.write('よみがな・背ネームは --assign-readings で付け直してください。')

    def _assign_readings(self, *, dry_run):
        """既存の氏名からよみがな・苗字ローマ字・名前ローマ字を復元し、背ネームを決める。

        新規に選び直すわけではなく、すでに確定している漢字名に対応する読みを
        プールから逆引きする。プールにない氏名（実在の元選手）は
        ORIGINAL_PLAYER_READINGS を使う。
        """
        surname_lookup = {kanji: (kana, romaji) for kanji, kana, romaji in JP_SURNAMES + RARE_JP_SURNAMES}
        given_lookup = {kanji: (kana, romaji) for kanji, kana, romaji in JP_GIVEN_NAMES + RARE_JP_GIVEN_NAMES}
        surnames_by_length = sorted(surname_lookup, key=len, reverse=True)

        foreign_given_lookup = {}
        foreign_surname_lookup = {}
        for group in FOREIGN_GROUPS:
            for kana, romaji in group['given']:
                foreign_given_lookup[kana] = romaji
            for kana, romaji in group['surname']:
                foreign_surname_lookup[kana] = romaji

        players = list(Player.objects.all())
        surname_romaji_by_id = {}
        given_romaji_by_id = {}
        name_kana_by_id = {}
        unresolved = []

        for player in players:
            if player.name in ORIGINAL_PLAYER_READINGS:
                kana, surname_romaji, given_romaji = ORIGINAL_PLAYER_READINGS[player.name]
            elif player.is_foreign_player and '・' in player.name:
                given_kana, surname_kana = player.name.split('・', 1)
                given_romaji = foreign_given_lookup.get(given_kana)
                surname_romaji = foreign_surname_lookup.get(surname_kana)
                kana = player.name
                if given_romaji is None or surname_romaji is None:
                    unresolved.append(player.name)
                    continue
            else:
                match = None
                for surname_kanji in surnames_by_length:
                    if player.name.startswith(surname_kanji):
                        given_kanji = player.name[len(surname_kanji):]
                        if given_kanji in given_lookup:
                            match = (surname_kanji, given_kanji)
                            break
                if match is None:
                    unresolved.append(player.name)
                    continue
                surname_kanji, given_kanji = match
                surname_kana, surname_romaji = surname_lookup[surname_kanji]
                given_kana, given_romaji = given_lookup[given_kanji]
                kana = surname_kana + given_kana

            name_kana_by_id[player.id] = kana
            surname_romaji_by_id[player.id] = surname_romaji
            given_romaji_by_id[player.id] = given_romaji

        # チームごとに同姓を数え、背ネームを決める（在籍していない選手は苗字のみ）。
        team_by_player_id = {
            s.player_id: s.team_id
            for s in PlayerStint.objects.filter(to_year__isnull=True)
        }
        counts_by_team = defaultdict(Counter)
        for player_id, surname_romaji in surname_romaji_by_id.items():
            team_id = team_by_player_id.get(player_id)
            counts_by_team[team_id][surname_romaji] += 1

        to_update = []
        for player in players:
            if player.id not in name_kana_by_id:
                continue
            team_id = team_by_player_id.get(player.id)
            surname_romaji = surname_romaji_by_id[player.id]
            given_romaji = given_romaji_by_id[player.id]
            if counts_by_team[team_id][surname_romaji] > 1:
                back_name = f"{given_romaji[0]}.{surname_romaji}"
            else:
                back_name = surname_romaji

            player.name_kana = name_kana_by_id[player.id]
            player.back_name = back_name
            to_update.append(player)

        if not dry_run:
            with transaction.atomic():
                Player.objects.bulk_update(to_update, ['name_kana', 'back_name'], batch_size=500)

        prefix = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}選手 {len(to_update)}人によみがな・背ネームを付与しました"
        ))
        if unresolved:
            self.stdout.write(self.style.WARNING(
                f"読みを特定できず未対応のまま: {len(unresolved)}人 {unresolved[:10]}"
            ))
