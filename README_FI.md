# Mango COP-tekoäly (AI Heat Pump Controller)

Home Assistantin integraatio, joka toimii Raspberry Pi:llä HA:n sisällä. Se vahtii VILP:n COP:ia minuutin välein ja säätää EEV:tä Mango Tuya Local -integraation kautta, kun data näyttää korjattavan vian.

- **Vahtii COP:ia.** Laskee COP:n kylmäpiiristä ja vesipuolelta ja suhteuttaa sen Carnot-COP:iin (*laatuaste*). Normaalitaso opitaan erikseen jokaiselle ulkolämpö- ja kierrosluokalle. Jos COP on tunnin yli 10 % opittua matalampi, tulee ilmoitus todennäköisestä syystä: märkä imu, huurre, höyrystin, lauhdutin, kylmäainevaje, sähkövastus tai vika.
- **Säätää vain EEV:tä.** Säädettävät parametrit ovat *Heat Min OD1–9* (EEV:n minimiavaus) ja *Heat Sv1–9* (tulistuksen tavoite). Säätö alkaa vain, kun imukaasu on märkää, ja yksi pieni askel kerrallaan.
- **Ei koske muuhun.** Varaajan lämpötila, sulatus, kompressorin nopeudet, puhallin, pumppu ja suojaukset jäävät rauhaan.

## Asennus (HA OS, Raspberry Pi)

1. Asenna integraatio jommallakummalla tavalla:
   - **HACS:** lisää `https://github.com/artepe/AI_heatpump_controller` mukautetuksi repositorioksi (tyyppi Integraatio) ja lataa **AI Heat Pump Controller**.
   - **Käsin:** lataa `mango_cop_ai_<versio>.zip` [Releases-sivulta](https://github.com/artepe/AI_heatpump_controller/releases) ja pura se Home Assistantin `config`-kansioon niin, että syntyy kansio `config/custom_components/mango_cop_ai/`. Voit käyttää Samba-jakoa, File editoria tai Terminal & SSH -lisäosaa: `cd /config && unzip -o mango_cop_ai_1.0.0.zip`.
2. Lisää `configuration.yaml`:n loppuun yksi rivi:

   ```yaml
   mango_cop_ai:
   ```

3. Kehittäjätyökalut → YAML → Tarkista asetukset, ja käynnistä HA uudelleen.
4. Lisää kojelautaan [`examples/kortti.yaml`](examples/kortti.yaml): Muokkaa kojelautaa → Lisää kortti → Manuaalinen → liitä. Zip-paketissa sama kortti on integraation kansiossa nimellä `kortti.yaml`.

Mango Tuya Local -integraation pitää olla asennettuna ja parametrien näkyvissä. Tekoäly löytää number-entiteetit itse niiden tunnisteista, joten entiteetti-id:itä ei tarvitse kirjoittaa.

## Käyttö

Tila valitaan entiteetillä **COP-tekoäly tila**. Oletuksena tekoäly käynnistyy tarkkailutilassa, ja sen jälkeen voimassa on viimeksi valittu tila myös uudelleenkäynnistysten yli.

| Tila | Mitä tekee |
|---|---|
| Tarkkailu | Laskee, oppii ja diagnosoi. Ei kirjoita mitään. Lokikirjaan tulee "Tekisin: …" ja perustelut. |
| Ehdota | Ilmoittaa, mitä haluaisi tehdä. Toteuttaa vasta, kun painat **Hyväksy ehdotus**. |
| Automaattinen | Tekee muutokset itse, varmistaa ne ja peruu tarvittaessa. |
| Pois | Ei tee mitään. Jos testi on kesken, testattu arvo palautetaan. |

Suositeltu järjestys: Tarkkailu 1–2 pakkasyötä → Ehdota → Automaattinen.

**Palauta alkuperäiset** kirjoittaa kaikki tekoälyn muuttamat arvot takaisin ja vaihtaa tilaksi Tarkkailu.

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

## Turvallisuus

- **Vain kaksi taulukkoa.** Mitään muuta tekoäly ei voi kirjoittaa.
- **Pienet askeleet ja rajat:**
  - Heat Min OD: askel 2 %, enintään 8 % alkuperäisestä alas, ei koskaan alle 8 %.
  - Heat Sv: askel 0,5 K, enintään 3 K ylös, ei koskaan yli 6 K.
  - Muutos menee aina vain alkuperäisestä poispäin ja takaisin, ei koskaan alkuperäisen ohi.
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
| `sensor.cop_tekoaly_tilanne` | Mitä tekoäly tekee nyt. Attribuutteina kesken oleva toimenpide, muutetut arvot, opitut vyöhykkeet ja historia. |
| `sensor.cop_tekoaly_diagnoosi` | Todennäköinen tila tai vika |
| `sensor.cop_tekoaly_cop` | Tunnin COP (kylmäpiiri). Attribuutteina vesipuoli, Carnot ja odotettu COP. |
| `sensor.cop_tekoaly_cop_poikkeama` | % opitusta tasosta |
| `sensor.cop_tekoaly_cop_laatuaste` | COP / Carnot-COP, % |
| `sensor.cop_tekoaly_naennainen_hyotysuhde` | Märkyyden mittari. Yli 85 % tarkoittaa märkää imua. |
| `sensor.cop_tekoaly_kirjoitukset_24_h` | Parametrikirjoitukset viimeisen vuorokauden aikana |
| `button.cop_tekoaly_hyvaksy_ehdotus` / `_hylkaa_ehdotus` / `_palauta_alkuperaiset` | Ehdotuksen hyväksyntä ja hylkäys sekä kaikkien arvojen palautus |

## Asetukset

Kaikki on valinnaista. Oletukset ovat tekijän Home Assistantin entiteettien nimet; vaihda omasi `anturit:`-kohtaan. Sama esimerkki on tiedostossa [`examples/configuration.yaml`](examples/configuration.yaml).

```yaml
mango_cop_ai:
  tila: Tarkkailu                 # vain ensimmäinen käynnistys
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
    vastus: binary_sensor.hp_electric_heater
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
```

Jos jokin vikatila-anturi on normaalikäytössä päällä, poista se listalta. Muuten tekoäly pysyy tauolla.

## Rajoitukset

- Mango-profiilissa näiden parametrien kirjoitusoikeutta ei ole vielä todennettu laitteella. Jos laite hylkää kirjoituksen, tekoäly huomaa sen ja jättää parametrin viikoksi rauhaan.
- Vyöhykkeiden rajat opitaan käytössä. Ensimmäinen haku samoissa oloissa voi viedä 1–9 kokeilua, jokainen 4–15 minuuttia.
- Vesipuolen COP voi olla mittakaavaltaan vinossa (tekijän laitteistossa noin 1,6-kertainen, kunnes ΔT- tai virtausvirhe korjataan). Tekoäly vertaa sitä vain omaan aiempaan tasoonsa, joten mittakaavavirhe ei haittaa.
- Yhteisöprojekti, ei valmistajan julkaisu. Integraatio muuttaa lämpöpumpun ohjaimen parametreja: käyttö omalla vastuulla, ja aloita aina tarkkailutilasta.
