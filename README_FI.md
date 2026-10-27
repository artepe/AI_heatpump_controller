# Mango COP-tekoäly (AI Heat Pump Controller)

Home Assistantin integraatio, joka toimii Raspberry Pi:llä HA:n sisällä. Se vahtii VILP:n COP:ia minuutin välein, säätää EEV:tä Mango Tuya Local -integraation kautta, kun data näyttää korjattavan vian, ja päättää, milloin ulkoyksikön kenno sulatetaan.

- **Vahtii COP:ia.** Laskee COP:n kylmäpiiristä ja vesipuolelta ja suhteuttaa sen Carnot-COP:iin (*laatuaste*). Normaalitaso opitaan erikseen jokaiselle ulkolämpö- ja kierrosluokalle. Jos COP on tunnin yli 10 % opittua matalampi, tulee ilmoitus todennäköisestä syystä: märkä imu, huurre, höyrystin, lauhdutin, kylmäainevaje, sähkövastus tai vika.
- **Säätää vain EEV:tä.** Säädettävät parametrit ovat *Heat Min OD1–9* (EEV:n minimiavaus) ja *Heat Sv1–9* (tulistuksen tavoite). Säätö alkaa vain, kun imukaasu on märkää, ja yksi pieni askel kerrallaan.
- **Sulattaa kennon, kun huurretta on kertynyt.** Huurteen määrä mitataan jatkuvasti, sulatus käynnistetään omalla pakkosulatusskriptilläsi, jokainen sulatus arvioidaan, ja säätimen omaa sulatusväliä säädetään. Katso [Sulatusohjaus](#sulatusohjaus).
- **Ei koske muuhun.** Varaajan lämpötila, kompressorin nopeudet, puhallin, pumppu, suojaukset ja sulatuksen lämpötilakynnykset jäävät rauhaan.

## Asennus (HA OS, Raspberry Pi)

1. Asenna integraatio jommallakummalla tavalla:
   - **HACS:** lisää `https://github.com/artepe/AI_heatpump_controller` mukautetuksi repositorioksi (tyyppi Integraatio) ja lataa **AI Heat Pump Controller**.
   - **Käsin, Terminal & SSH -lisäosassa:** lataa uusin zip [Releases-sivulta](https://github.com/artepe/AI_heatpump_controller/releases) ja pura se `config`-kansioon niin, että syntyy kansio `config/custom_components/mango_cop_ai/`:

     ```bash
     cd /config && curl -fsSL -o /tmp/mango_cop_ai.zip https://github.com/artepe/AI_heatpump_controller/releases/download/v1.2.0/mango_cop_ai_1.2.0.zip && unzip -o /tmp/mango_cop_ai.zip && ha core check && ha core restart
     ```

2. Lisää `configuration.yaml`:n loppuun yksi rivi:

   ```yaml
   mango_cop_ai:
   ```

3. Kehittäjätyökalut → YAML → Tarkista asetukset, ja käynnistä HA uudelleen.
4. Lisää kojelautaan kortti: Muokkaa kojelautaa → Lisää kortti → hae **COP-tekoäly**, tai Manuaalinen → `type: custom:cop-tekoaly-card`. Katso [Kojelautakortti](#kojelautakortti).
   Markdown-pohjainen teknisempi kortti on edelleen [`examples/kortti_tekninen.yaml`](examples/kortti_tekninen.yaml): COP kummaltakin puolelta, imun märkyys, käynnissä oleva toimenpide, EEV:n 9 vyöhykkeen taulukko tekoälyn muutoksineen, kylmäpiirin arvot, käyrät ja päätöshistoria. Fontti on tasalevyinen, jos card-mod on asennettu.

Mango Tuya Local -integraation pitää olla asennettuna ja parametrien näkyvissä. Tekoäly löytää number-entiteetit itse niiden tunnisteista, joten entiteetti-id:itä ei tarvitse kirjoittaa.

## Kojelautakortti

Integraatio tuo mukanaan oman kortin ja lataa sen itse jokaiselle kojelaudalle. Resurssia ei tarvitse lisätä, eikä HACS-korttia asentaa.

```yaml
type: custom:cop-tekoaly-card
```

Kortti näyttää ylhäältä alas:

- mitä tekoäly tekee nyt, ja diagnoosin, kun siinä on jotain kerrottavaa
- odottavan ehdotuksen ja painikkeet **Hyväksy ehdotus** ja **Hylkää**
- **Miksi tekoäly ei säädä nyt**: jokainen este omalla rivillään ja mitä asialle voi tehdä. *Korjaa* tarkoittaa, että asia vaatii sinulta toimia, *Odottaa* korjaantuu itsestään ja *Tieto* johtuu valitusta tilasta.
- imukaasun märkyyden näkölasin: kompressorin näennäinen hyötysuhde, kuivan imun taso ja märkäraja. Kun imu on märkää, lasi täyttyy kylmäaineen sinisellä.
- COP:n, laatuasteen, poikkeaman opitusta tasosta ja kirjoitukset 24 h. Lukemaa napauttamalla avautuu historia.
- EEV-säädön ja sulatusohjauksen tilat (Pois, Tarkkailu, Ehdota, Automaattinen)
- ulkoyksikön kennon tilan, sulatustarpeen ja viimeisimmän sulatuksen
- tekoälyn muuttamat arvot ja viimeisimmät päätökset
- **Palauta alkuperäiset**, kun palautettavaa on. Painike kysyy vahvistuksen toisella painalluksella.

Valinnat:

```yaml
type: custom:cop-tekoaly-card
title: VILP-tekoäly       # otsikko, oletus "COP-tekoäly"
show_controls: true       # tilat, hyväksy/hylkää ja palautus
show_defrost: true        # kennon ja sulatuksen tila
show_history: true        # päätöshistoria
```

## Jos tekoäly ei tee mitään

Katso kortin kohta **Miksi tekoäly ei säädä nyt**. Sama luettelo on anturin `sensor.cop_tekoaly_tilanne` attribuutissa `esteet`. Yleisimmät syyt:

| Este | Mitä tehdä |
|---|---|
| Tarkkailutila | Tarkkailu ei muuta mitään, se kertoo vain lokikirjassa, mitä tekisi. Vaihda tilaksi Ehdota tai Automaattinen. |
| Pakollinen mittaus puuttuu tai ei päivity | Entiteetin nimi on eri kuin oletus tai sitä ei ole. Korjaa nimi `anturit:`-kohtaan. |
| Mango-parametreja ei löydy tai saatavilla x/y | Mango Tuya Local ei ole asennettu, sen EEV Set -parametrit on poistettu käytöstä tai yhteys laitteeseen puuttuu. |
| Tasaista käyntiä n/27 min | Päätökset tehdään vain tasaisesta käynnistä. Rivillä lukee, miksi nykyinen minuutti ei kelpaa, esimerkiksi kompressori käynnistyi juuri tai sulatus on käynnissä. |
| Seuraava muutos aikaisintaan / kirjoitusraja | Edellisen muutoksen jälkeinen tauko tai 12 kirjoitusta vuorokaudessa täynnä. |
| Ei säädettävää | Imukaasu ei ole märkää, joten korjattavaa ei ole. |

Versio 1.2.0 korjasi kolme vikaa, joiden takia tekoäly saattoi jäädä odottamaan ikuisesti:

- **Muutoksista raportoivat anturit tulkittiin vanhentuneiksi.** Template-, ESPHome- ja MQTT-anturit kirjoittavat tilansa vain, kun se muuttuu. Tunteja pois päällä ollut sulatusanturi näytti siksi vanhentuneelta, ja jokainen minuutti hylättiin. Nyt ikäraja koskee vain Modbus-entiteettejä, jotka kirjoittavat arvonsa jokaisella kyselyllä.
- **Sulatusanturin nimi.** Jos `binary_sensor.hp_defrost_active` puuttui, kaikki pysähtyi. Nyt tilalla käytetään Modbusin sulatusventtiiliä `binary_sensor.hp_m0_defrost_venttiili`.
- **Lyhyet käyntijaksot.** Lauhalla säällä kompressori käy esimerkiksi 40 min ja seisoo 20 min. Jokainen käynnistys vie 15 min tasaantumiseen, joten 45 minuutin ikkunaan ei koskaan kertynyt 27 tasaista minuuttia. Nyt saman ulkolämmön aiempien käyntijaksojen tasaiset minuutit lasketaan yhteen.

## Käyttö

Tila valitaan kortista tai entiteetillä **COP-tekoäly tila**. Oletuksena tekoäly käynnistyy tarkkailutilassa, ja sen jälkeen voimassa on viimeksi valittu tila myös uudelleenkäynnistysten yli. Jos muutat `tila:`- tai `sulatus_tila:`-rivin arvoa `configuration.yaml`:ssa, uusi arvo otetaan käyttöön seuraavassa käynnistyksessä.

| Tila | Mitä tekee |
|---|---|
| Tarkkailu | Laskee, oppii ja diagnosoi. Ei kirjoita mitään. Lokikirjaan tulee "Tekisin: …" ja perustelut. |
| Ehdota | Ilmoittaa, mitä haluaisi tehdä. Toteuttaa vasta, kun painat **Hyväksy ehdotus**. |
| Automaattinen | Tekee muutokset itse, varmistaa ne ja peruu tarvittaessa. |
| Pois | Ei tee mitään. Jos testi on kesken, testattu arvo palautetaan. |

Suositeltu järjestys: Tarkkailu 1–2 pakkasyötä → Ehdota → Automaattinen.

Sulatusohjauksella on oma tilansa, **COP-tekoäly sulatustila**, samoilla vaihtoehdoilla. Se alkaa myös tarkkailutilassa, joten EEV-säädön voi pitää automaattisena ja katsoa sulatuksia ensin rauhassa.

**Palauta alkuperäiset** kirjoittaa kaikki tekoälyn muuttamat arvot takaisin, myös sulatusvälit, ja vaihtaa molemmat tilat tarkkailuun.

## Miten päätös syntyy

1. **Märkä imu tunnistetaan kompressorin näennäisestä hyötysuhteesta.** Arvo lasketaan höyrystymis- ja lauhtumislämmöstä, imu- ja kuumakaasusta R290:n entalpioilla, samoin kuin kylmäpiirin COP-anturissa. Kuivalla imulla näennäinen hyötysuhde on noin 0,6–0,75. Märkä kaasu näyttää kylmäainelukemissa kuivalta, joten lukema nousee lähelle 1:tä (19.10. yöllä lähes 1,0). Kuivan imun taso opitaan jaksoista, joilla tulistus on yli 3 K. Siihen asti oletus on 0,70 ja märkäraja 0,85.
2. **Toimenpide valitaan tilanteen mukaan:**
   - EEV jumittaa minimiavauksessa ja imu on märkää: aktiivisen vyöhykkeen *Heat Min OD* −2 %.
   - EEV säätää vapaasti mutta imu on märkää: aktiivisen vyöhykkeen *Heat Sv* +5 (0,5 K).
   - EEV alkaa heilua lasketun minimin päällä: minimi nostetaan askel takaisin, eikä sitä enää lasketa sen alle.
   - Imu on pysynyt 3 h kuivana: tekoäly laskee itse nostamaansa *Heat Sv* -arvoa askel kerrallaan takaisin, jotta höyrystymislämpö nousee.
3. **Aktiivinen vyöhyke etsitään kokeilemalla.** Ohjaimen vyöhyketaulukon rajoja ei tunneta. Tekoäly muuttaa yhtä ehdokasta ja katsoo, reagoiko ohjain:
   - Heat Min OD: EEV:n lattia siirtyy.
   - Heat Sv: tulistuksen tavoite (`sensor.hp_subcool_superheat_controller`) siirtyy.

   Jos ohjain ei reagoi, arvo palautetaan ja kokeillaan seuraavaa. Ensimmäinen arvaus olettaa vyöhykkeet ulkolämpötilan mukaan lämpimästä (1) kylmään (9). Löydetty vyöhyke tallennetaan, ja seuraavalla kerralla samoissa oloissa mennään suoraan siihen.
4. **Varmistus.** Muutoksen vaikutusta seurataan 60 tasaisen käyntiminuutin ajan. Sulatus, öljynpalautusbuusti ja käynnistys eivät kelpaa vertailuun.
   - Muutos jää voimaan, jos imu kuivui tai EEV tasaantui.
   - Muutos perutaan, jos hyötyä ei tullut, ja samaan parametriin ei kosketa viikkoon.
   - Muutos perutaan heti, jos ilmenee haittaa: kuumakaasu yli 100 °C, höyrystin nälkiintyy (lähestymä kasvaa yli 6 K), EEV heiluu tai imu kastuu uudelleen. Silloin parametria ei kosketa kahteen viikkoon.

Kylmäpiirin COP-lukema laskee, kun imu kuivuu. Se johtuu siitä, että märkä imu näyttää lukeman liian suurena. Vesipuolen COP-laatuaste on silloin parempi vertailukohta, ja ilmoitukset näyttävät molemmat.

## Sulatusohjaus

Mangossa on 39 sulatusparametria, mutta suoraa "sulata nyt" -käskyä ei ole. Parametrien merkitys on päätelty niiden nimistä. Siksi tekoäly mittaa huurteen itse, käynnistää sulatuksen omalla pakkosulatusskriptilläsi ja kirjoittaa säätimeen vain sulatusvälejä, joiden vaikutuksen se näkee seuraavista sulatuksista.

1. **Huurre mitataan jatkuvasti.**
   - Huurre eristää kennon ja tukkii ilman kulun, joten höyrystimen lähestymä (ulkoyksikön ilma − höyrystymislämpö) kasvaa.
   - Puhtaan kennon taso mitataan jokaisen sulatuksen jälkeen 12–60 käyntiminuutin kohdalta, tasaisella nopeudella.
   - Taso skaalataan kompressorin ja puhaltimen nopeudella. Kasvu siitä eteenpäin on huurteen määrä kelvineinä. Mukaan lasketaan myös ennen mittausta kertynyt huurre samalla nopeudella, enintään 2,5 K.
   - Äkillinen hyppy, esimerkiksi hiljaisen tilan puhallinmuutos, ei ole huurretta, joten se siirtää vertailutasoa.
   - Pysyvä nopeusmuutos mitataan uudelta tasolta niin, että huurrearvio jatkuu katkeamatta.
   - **Sulatustarve 100 %** tarkoittaa, että huurretta on tekoälyn kynnyksen verran, aluksi 4 K, eli noin 10 % COP-häviö.
2. **Tekoäly sulattaa itse, kun tarve on 100 %.** Sulatus käynnistetään pakkosulatusskriptilläsi (`script.sulatus_pakotettu_moduuli_0`). Jos sitä ei löydy, käytetään skriptiä, jonka nimessä on "Sulatus pakotettu". Ehdot:
   - edellisestä sulatuksesta vähintään 25 käyntiminuuttia
   - ulkoilma enintään +8 °C
   - tasainen käynti, ei vikaa
   - enintään 24 pakkosulatusta vuorokaudessa

   Sulatusanturin pitää mennä päälle 6 minuutissa. Jos se ei mene kahdesti, pakkosulatus on tauolla vuorokauden ja siitä tulee ilmoitus. Jos uusi sulatus alkaa heti pakkosulatuksen perään, skripti saattaa jättää pakotuksen päälle, joten sekin pysäyttää pakkosulatukset vuorokaudeksi.
3. **Jokainen sulatus arvioidaan:**

   | Arvio | Peruste |
   |---|---|
   | turha | Kenno oli lähes puhdas: huurretta alle 1 K ja sulatus lyhyt. Tekoälyn oma sulatus on turha, jos se kesti vain puhtaan kennon sulatusajan. |
   | sopiva | Tältä väliltä. |
   | myöhäinen | Huurretta 1,5 × kynnys (6 K) tai COP-häviö yli 15 %, tai sulatus kesti selvästi tavallista kauemmin. |
   | kesken | Sulatus kesti säätimen maksimiajan (Dfr.Time) eikä lamelli lämmennyt loppulämpöön (Dfr.Coil T). |
   | epäselvä | Puhtaan kennon tasoa ei ehditty mitata, esimerkiksi heti uudelleenkäynnistyksen jälkeen. |

   Lisäksi kirjataan, mikä sulatuksen laukaisi:
   - **tekoäly**
   - **sulatusväli**: käyntiaika edellisestä sulatuksesta vastasi Dfr.Interval- tai kaistan Dfr.Inte. n -arvoa (+ T. Sure Time)
   - **lämpötilaehto**
   - **hätäsulatus**

   Kaistat 1 (lämmin) … 5 (kylmä) tulevat säätimen rajoista Dfr.Env.T.1–4 ulkoyksikön omalla ulkoanturilla.
4. **Oma kynnys kalibroituu.**
   - Jos tekoälyn sulatus osoittautuu turhaksi, kynnys nousee 0,5 K.
   - Jos säätimen oma sulatus on myöhäinen ennen kuin tekoäly piti sitä tarpeellisena, kynnys laskee 0,5 K.
   - Kynnys pysyy välillä 2,5–8 K.
5. **Säätimen sulatusväliä säädetään yksi askel kerrallaan.** Vain sitä väliä muutetaan, joka sulatukset laukaisi:
   - **pidennys +5 min**, kun kaistan neljästä viimeisestä sulatuksesta kolme oli turhia ja saman välin laukaisemia, eikä yksikään ollut myöhäinen. Kun pakkosulatus on käytössä, tekoäly sulattaa itse, jos huurretta kertyy.
   - **lyhennys −5 min**, kun kolmesta viimeisestä kaksi oli myöhäisiä ja odottivat väliä. Yksi riittää, jos väli on tekoälyn itse pidentämä.
   - Muutos varmistetaan kaistan kahdesta seuraavasta sulatuksesta: välin pitää muuttua asetuksen mukana, eikä sulatus saa olla myöhäinen tai kesken. Muuten alkuperäinen palautetaan. Haitan jälkeen väliä ei kosketa kahteen viikkoon, ja jos vaikutusta ei näy, viikkoon.
   - Rajat: enintään 30 min alkuperäistä pidempi ja 15 min lyhyempi, aina välillä 30–120 min.
6. **Muut havainnot ovat suosituksia.** Esimerkiksi myöhään alkavat lämpötilaehdon sulatukset (Dfr.T.Diff / Frost D1) tai turhat sulatukset yli +4 °C:ssa (Dfr. Env.T). Ne näkyvät anturin `sensor.cop_tekoaly_sulatus` attribuutissa `suositukset`, eikä tekoäly kirjoita niitä.

Jokainen sulatus kirjataan lokikirjaan, esimerkiksi:

> Sulatus 4 min 00 s, käyntiä edellisestä 61 min, ulkona −0,6 °C (kaista 3): sopiva – huurretta 4,1 K. Tekoälyn pakkosulatus.

## Turvallisuus

- **Vain kaksi EEV-taulukkoa ja sulatusvälit.** Muuta tekoäly ei voi kirjoittaa: *Heat Min OD1–9*, *Heat Sv1–9*, *Dfr.Interval* ja *Dfr.Inte. 1–5*. Lisäksi se voi käynnistää pakkosulatusskriptin.
- **Pienet askeleet ja rajat:**
  - Heat Min OD: askel 2 %, enintään 8 % alkuperäisestä alas, ei koskaan alle 8 %.
  - Heat Sv: askel 0,5 K, enintään 3 K ylös, ei koskaan yli 6 K.
  - Muutos menee aina vain alkuperäisestä poispäin ja takaisin, ei koskaan alkuperäisen ohi.
  - Sulatusväli: askel 5 min, enintään +30 / −15 min alkuperäisestä, aina 30–120 min.
- **Kirjoitusraja.** Enintään 12 kirjoitusta vuorokaudessa, mikä säästää ohjaimen muistia. Palautukset sallitaan aina.
- **Vika keskeyttää.** Jos jokin vikatila-antureista on päällä (`binary_sensor.hp_fault_active`, `hp_serious_alarm`, matala- ja korkeapainevika, kuumakaasun ylilämpö), kaikki tekoälyn muuttamat arvot palautetaan ja säädöt pidetään 24 h tauolla.
- **Käsin tehty muutos voittaa.** Jos muutat tekoälyn säätämää arvoa Mango Controlissa, tekoäly ottaa arvon uudeksi lähtökohdaksi eikä koske siihen viikkoon.
- **Kirjoitukset varmistetaan.** Jokainen kirjoitus kulkee Mango-integraation kautta. Se tarkistaa tuoreet rajat, lukee arvon takaisin ja palauttaa alkuperäisen, jos laite ei vahvista. Jos laite hylkää parametrin, tekoäly jättää sen viikoksi rauhaan.
- **Uudelleenkäynnistys on turvallinen.** Jos HA käynnistyy uudelleen kesken kokeilun, kokeiltu arvo palautetaan. Kesken oleva varmistus jatkuu alusta.
- **Kaikki kirjataan.** Jokainen päätös perusteluineen näkyy lokikirjassa (COP-tekoäly), ja tärkeimmistä tulee ilmoitus.

## Entiteetit

| Entiteetti | Sisältö |
|---|---|
| `select.cop_tekoaly_tila` | Pois / Tarkkailu / Ehdota / Automaattinen |
| `sensor.cop_tekoaly_tilanne` | Mitä tekoäly tekee nyt. Attribuutteina esteet (miksi se ei säädä nyt), kesken oleva toimenpide, muutetut arvot, opitut vyöhykkeet ja historia. |
| `sensor.cop_tekoaly_diagnoosi` | Todennäköinen tila tai vika |
| `sensor.cop_tekoaly_cop` | Tunnin COP (kylmäpiiri). Attribuutteina vesipuoli, Carnot ja odotettu COP. |
| `sensor.cop_tekoaly_cop_poikkeama` | % opitusta tasosta |
| `sensor.cop_tekoaly_cop_laatuaste` | COP / Carnot-COP, % |
| `sensor.cop_tekoaly_naennainen_hyotysuhde` | Märkyyden mittari. Yli 85 % tarkoittaa märkää imua. |
| `sensor.cop_tekoaly_kirjoitukset_24_h` | Parametrikirjoitukset viimeisen vuorokauden aikana |
| `select.cop_tekoaly_sulatustila` | Sulatusohjauksen tila: Pois / Tarkkailu / Ehdota / Automaattinen |
| `sensor.cop_tekoaly_sulatustarve` | Huurteen määrä, % tekoälyn kynnyksestä (100 % = sulatus nyt). Attribuutteina kennon tila, huurre K, COP-häviö, käyntiaika sulatuksesta, arvio sulatustarpeeseen ja oma kynnys. |
| `sensor.cop_tekoaly_sulatus` | Viimeisimmän sulatuksen arvio. Attribuutteina 10 viimeistä sulatusta, 24 h luvut, kaistatilastot 7 vrk, sulatuksen laukaisseet välit, säätimen sulatusparametrit, muutetut välit ja suositukset. |
| `button.cop_tekoaly_hyvaksy_ehdotus` / `_hylkaa_ehdotus` / `_palauta_alkuperaiset` | Ehdotuksen hyväksyntä ja hylkäys sekä kaikkien arvojen palautus |

## Asetukset

Kaikki on valinnaista. Oletukset ovat tekijän Home Assistantin entiteettien nimet; vaihda omasi `anturit:`-kohtaan. Sama esimerkki on tiedostossa [`examples/configuration.yaml`](examples/configuration.yaml).

```yaml
mango_cop_ai:
  tila: Tarkkailu                 # EEV-säätö: otetaan käyttöön, kun arvo muuttuu tässä
  sulatus_tila: Tarkkailu         # sulatusohjaus: samoin
  # pakkosulatus: script.sulatus_pakotettu_moduuli_0   # oletuksena tämä tai "Sulatus pakotettu" -niminen
  anturit:                        # vaihda, jos nimesi ovat toiset
    sahko: sensor.invt_pout_power
    nopeus: sensor.hp_running_speed
    hoyrystyminen: sensor.kylmaaine_haihdutuksen_kyllastyslampo_evap_sat
    lauhtuminen: sensor.kylmaaine_lauhdutuksen_kyllastyslampo_cond_sat
    imukaasu: sensor.kylmaaine_imukaasu_suction
    kuumakaasu: sensor.kylmaaine_puristuskaasu_discharge
    eev: sensor.eev_main_opening
    tulistus: sensor.hp_main_eev_superheat_actual
    tulistus_tavoite: sensor.hp_subcool_superheat_controller
    ulko: sensor.ouman_outdoor_temperature
    ulko_varalla: sensor.ulkolampotila
    menovesi: sensor.lampopumppu_menovesi
    paluuvesi: sensor.lampopumppu_paluuvesi
    vesilampo: sensor.hp_lampoteho_laskettu
    huurre: sensor.vilp_huurre_cop_havio
    kompressori: binary_sensor.hp_m0_kompressori_rele
    sulatus: binary_sensor.hp_defrost_active
    sulatus_varalla: binary_sensor.hp_m0_defrost_venttiili  # käytetään, jos edellistä ei ole
    vastus: binary_sensor.hp_electric_heater
    lamelli: sensor.kylmaaine_lamellilampo_fin              # sulatus: kennon lämpö
    ulkoyksikko: sensor.hp_module_ambient_temperature      # sulatus: ulkoyksikön oma ulkoanturi
    huurteeton_ero: sensor.hp_frost_free_coil_ambient_delta
    virtaus: sensor.module_0_vf_water_pump_flow             # l/h, sulatuksen vedestä ottama lämpö
    puhallin: sensor.hp_fan_1_rpm_actual
  vikatilat:                      # mikä tahansa päällä = palautus ja 24 h tauko
    - binary_sensor.hp_fault_active
    - binary_sensor.hp_serious_alarm
    - binary_sensor.hp_fault_low_pressure
    - binary_sensor.hp_fault_high_pressure
    - binary_sensor.hp_fault_discharge_temperature_high
  asetukset:
    kirjoituksia_vrk: 12          # parametrikirjoituksia vuorokaudessa
    varmistus_min: 60             # tasaisia minuutteja ennen kuin muutos hyväksytään
    ikkuna_min: 45                # tasaista käyntiä ennen päätöstä
    kuumakaasu_max: 100           # °C, tätä kuumempi kaasu peruu muutoksen
    min_od_askel: 2               # %
    min_od_alaraja: 8             # %
    min_od_max_lasku: 8           # % alkuperäisestä
    sv_askel: 5                   # 0,1 K
    sv_max_nosto: 30              # 0,1 K alkuperäisestä
    sv_ylaraja: 60                # 0,1 K
    saada_min_od: true
    saada_sv: true
    poikkeama_raja: 10            # % COP-varoitukseen
    cop_lahde: kylmapiiri         # tai vesipuoli: kumpi COP näytetään anturissa
    vesipuolen_kerroin: 1.0       # esim. 0.62, jos virtaus näyttää 1,6-kertaisesti
    kuivan_imun_hyotysuhde: 0.70  # lähtöarvo, kunnes kuiva taso on opittu
    markyysraja: 0.15             # märkä, kun näennäinen hyötysuhde > kuiva taso + tämä
    # eev_askeleet: 500           # askeleita 100 %:ssa; oletuksena luetaan M.EEV Max stepistä
    saada_sulatus: true           # sulatusohjaus (pakkosulatus ja sulatusvälit)
    huurrekynnys: 4.0             # K, tekoälyn sulatuskynnyksen lähtöarvo (kalibroituu 2,5–8 K)
    puhdas_kenno: 1.0             # K, tätä vähemmällä huurteella sulatus oli turha
    sulatusvali_askel: 5          # min
    sulatusvali_max_nosto: 30     # min alkuperäisestä
    sulatusvali_max_lasku: 15     # min alkuperäisestä
    sulatusvali_ylaraja: 120      # min
    sulatusvali_alaraja: 30       # min
```

Jos jokin vikatila-anturi on normaalikäytössä päällä, poista se listalta. Muuten tekoäly pysyy tauolla.

## Rajoitukset

- Mango-profiilissa näiden parametrien kirjoitusoikeutta ei ole vielä todennettu laitteella. Jos laite hylkää kirjoituksen, tekoäly huomaa sen ja jättää parametrin viikoksi rauhaan.
- Sulatusparametrien merkitykset on päätelty nimistä. Tekoäly kirjoittaa vain sulatusvälejä ja vain silloin, kun on nähnyt niiden laukaisevan sulatukset. Jos väli ei muutu asetuksen mukana, muutos perutaan.
- Pakkosulatusskriptin toiminta riippuu siitä, mitä se tekee laitteelle. Tekoäly varmistaa jokaisen pakkosulatuksen sulatusanturista.
- Vyöhykkeiden rajat opitaan käytössä. Ensimmäinen haku samoissa oloissa voi viedä 1–9 kokeilua, jokainen 4–15 minuuttia.
- Vesipuolen COP voi olla mittakaavaltaan vinossa (tekijän laitteistossa noin 1,6-kertainen, kunnes ΔT- tai virtausvirhe korjataan). Tekoäly vertaa sitä vain omaan aiempaan tasoonsa, joten mittakaavavirhe ei haittaa.
- Yhteisöprojekti, ei valmistajan julkaisu. Integraatio muuttaa lämpöpumpun ohjaimen parametreja: käyttö omalla vastuulla, ja aloita aina tarkkailutilasta.

## Mittausten kunnon seuranta

`sensor.cop_tekoaly_mittausten_tila` näyttää tilan **Odottaa mittauksia**,
**Kunnossa** tai **Puutteellinen**. Attribuutti `ohjauksen_estävät_mittaukset`
luettelee puuttuvat, virheelliset tai jumiutuneet pakolliset mittaukset, ja
`viimeisin_päivitys` kertoo viimeisimmän käsittelyajan. Valinnainen puuttuva
anturi näkyy attribuutissa `puuttuvat_entiteetit`, mutta ei yksin estä ohjausta.

Jumiutunut lukema tunnistetaan vain Modbus-entiteeteistä. Modbus kirjoittaa
arvon jokaisella kyselyllä silloinkin, kun se ei muutu, joten 15 minuutin
hiljaisuus tarkoittaa, että kysely on pysähtynyt. Template-, ESPHome- ja
MQTT-anturit kirjoittavat tilansa vain muuttuessaan, joten niiden pitkä
hiljaisuus on normaalia eikä estä mitään. Myös tuntematon tai puuttuva kompressorin tai
sulatuksen tila estää tasaiseksi käynniksi hyväksymisen. Mittausten palautuessa
seuranta jatkuu automaattisesti.
