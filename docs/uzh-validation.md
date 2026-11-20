# UZH bookings vs. the course catalogue

Issue #14. Checked on 2026-11-20 for the teaching week Mon 23 – Fri 27 Nov 2026 (HS 2026, week 48).

**Result: safe to ship.** Every course meeting that the catalogue puts into a room we show was a `belegt` block in 3V-Rooms at exactly the same time, except one 45-minute journal club (see [the one miss](#the-one-miss)). Cancelled courses lose their 3V booking, so their room correctly shows as free.

| Scope | Rooms | Catalogue meetings | Matched | Missing in 3V |
|---|---|---|---|---|
| 10 sample rooms (tables below) | 10 | 165 | 165 | 0 |
| All rooms on our list (`uzh_rooms.txt`) | 133 with teaching that week | 1917 | 1916 | 1 |

## Method

**3V-Rooms.** We used `freerooms.uzh.UzhClient` (anonymous session, 1 s between requests). For Monday we fetched each campus view (Zentrum 24885, Irchel 22059, Oerlikon 15325), which also gives the room list. For Tue–Fri we fetched all 193 room resources by id. We also fetched 16–20 Nov, 30 Nov – 4 Dec and a few holidays to cross-check. Times are Zurich wall-clock time, as described in `uzh.py`.

**Catalogue.** courses.uzh.ch redirects to a SAP UI5 app (`studentservices.uzh.ch/uzh/anonym/vvz/`). It has no room search, but it has two public, anonymous OData services:

- **Search** (OData v4): `https://studentservices.uzh.ch/sap/opu/zodatav4/sap/zcm_vvz_v4_ui/srvd/sap/zsb_vvz/0001/ESearch`. This is the "Veranstaltungen" search of the `vvzsearch` app. With `$filter=AcademicYear eq '2026' and AcademicPeriod eq '003'` (HS 2026), it returns all 5,297 courses with `RoomText`, `ScheduleSummaryText` and `$expand=_Schedule($filter=EventDate ge … and EventDate le …)`, which gives the dates and times of each meeting. It does **not** give a room for each meeting: `RoomText` is a summary, which can be a single room, `siehe Details` or `s.Hinweise`. You cannot filter by room, so we downloaded all courses (11 pages of 500).
- **Course detail** (OData v2): `https://studentservices.uzh.ch/sap/opu/odata/uzh/vvz_data_srv/EDetailsSet(EObjId='…',PiqYear='2026',PiqSession='003')?$expand=Rooms,Persons,Schedule,Schedule/Rooms,Schedule/Persons,Modules,Links`. This lists every meeting with its own rooms. The full `$expand` is needed: `Schedule` comes back empty with a shorter expand, and `Rooms` alone gives HTTP 501.

In week 48, 3,617 courses meet. We fetched the detail of about 1,000 of them, about one per second. These were all courses that have a time but more than one room or no room in the summary, plus all courses in the 10 sample rooms and every course that did not match at first. For the other single-room courses we took the summary room for every meeting.

**Classification.** A catalogue meeting is *matched* if the room has a type `1` (`belegt`) block with the same start and end. A meeting is *missing* if some minute of it is covered by neither a `1` nor an `S` block, which means we would show the room as free. A block with no catalogue meeting is *in 3V only*. That is fine: exams, events, external bookings, and courses whose room is only in a free-text note.

## Findings

1. **Times match exactly.** All 1,916 matched meetings have the same start and end on both sides, to the minute. There is no setup or cleanup time and no rounding: a 13:00–13:45 course is a 13:00–13:45 block. Many 3V comments refer to course numbers ("Ref-Nr. 4000", "Tausch mit VL-Nr. 4931"), so 3V seems to be fed from the same room booking that the catalogue shows.
2. **Use the room of each meeting, not the summary.** At first, 20 meetings looked missing (in AFL-F-121, AND-2-04, KOL-G-222, …). In every case but one, the course detail has *no room* for that date. For example, "Armut und Reichtum" has AFL-F-121 only on 19.10 and 14.12, and "Writers Workshop" says "findet online statt". The summary `RoomText` still names the room. So the room was released, and 3V correctly shows it free.
3. **Cancelled courses disappear from 3V.** Courses marked "findet nicht statt" keep their dates in the catalogue, but the meetings have no room. Their slots were free in 3V in RAA-E-21, RAK-E-6 and SOE-F-8, and in GLT-A-03 someone else booked part of the slot (15:00–15:45). In one case, AFL-E-022 on Tue 10:15, the slot was still booked in 3V. That is the safe direction: the room shows as busy.
4. **Combined rooms.** A booking of a combination (`BIN-0-K.11/12/13`, `AND-3-02/06`, `Y22-F-62/68/70`) also shows up as `belegt` on each part. We checked 14 meetings, and all parts were busy. This confirms that we can drop combinations from `uzh_rooms.txt`.
5. **Blocks only in 3V** (23 in the sample rooms) are mostly evening events (18:00–21:45), a few daytime bookings and the standing `S` block on KOL-G-201. None of them matter for safety.
6. **KOL-G-201 Aula** has a standing `S` block "Informationen über die Verfügbarkeit auf Anfrage." from 11 Mar 2026 to 31 Dec 2030. It is the only room with one. It also has normal bookings that match the catalogue (8 of 8), but because of the `S` block we always show it as busy. That is safe, if not very useful.

### The one miss

Journal Club `07KOINI702.1`, Wed 25 Nov 12:15–13:00. The catalogue lists two rooms for that meeting, Y17-M-05 and Y55-G-54. Y55-G-54 is not in 3V. Y17-M-05 has the 12:15–13:00 block on 18 Nov but nothing on 25 Nov, and Y17-M-05 is otherwise free from 09:45 to 13:00 that day. Most likely the meeting moved or was cancelled and the catalogue was not updated. It is one 45-minute meeting out of 1,917.

## Rooms that 3V does not cover

About 1,020 catalogue meetings that week are in rooms that are not in the 3V campus views. That excludes 13 meetings whose room is spelled differently (`BIN-0-K.11..`, `RAA-G-01Aula`, `Y22-F-62/..`); we checked those by hand and all 13 are booked in 3V. Most of the 1,020 are in department and institute rooms that are booked elsewhere: PLH, PLG, ZUB, KIR, RAG, SOD-0-002, most of Irchel Y25/Y27/Y36, computer rooms, labs. Some are "siehe Hinweise" (177), "ETH-Raum" (71) or "Exkursion". **This is not a risk.** We never show these rooms, so we can't call them free.

Some catalogue rooms *are* in 3V but we drop them on purpose:

- **"EV" rooms** with courses: KO2-F-152 (4 meetings), KO2-F-173 (4), KOL-G-217 (3), KOL-H-309 (3), KO2-F-151 (2), KOL-G-210, KOL-G-212, KOL-E-13 (1 each). The KOL-E-13 course is in 3V. We drop them as event rooms, which is fine. We could add them later if they are open for studying.
- **Labs** Y14-F-21 and Y14-F-41: dropped on purpose.

## What `F` means

`F` is a **UZH closure** (holiday or building closure). It runs 00:00–23:59, or from a given time to 23:59. It applies to all 193 rooms when the whole university is closed, and to some rooms when only a building is. Examples from the current data:

| Day | `F` block | Rooms |
|---|---|---|
| Thu 24 Dec 2026 | "Heilig Abend UZH geschlossen" | all 193 |
| Mon 28 Dec 2026 | "Weihnachtsferien UZH geschlossen" | all 193 |
| Sat 2 Jan 2027 | "Berchtoldstag UZH geschlossen" | all 193 |
| Mon 4 Jan 2027 | "SOE geschlossen, mit Reglement nutzbar" | 11 (SOE building) |
| Thu 25 Mar 2027 | "Gründonnerstag 16:00 Uhr geschlossen" / "… 16:30 Uhr …" | 76 / 192 |

This matches the [UZH semester dates](https://www.uzh.ch/de/studies/dates/dates.html): the Easter break starts "25. März 2027 ab 16.00 Uhr". The week we checked (23–27 Nov) has no `F` blocks, and the semester dates list no closures in HS 2026. Treating `F` as *closed* is right. Note that `F` days can still have a few `1` bookings (111 on 24 Dec, 11 on 28 Dec). Showing those rooms as closed is fine.

Related: the GLT rooms have *`S`* blocks "Semesterferien GLT geschlossen (25.12.2026 - 21.02.2027)". It is a closure, but because it is `S` we show it as busy rather than closed. That is safe, and only the label is slightly off.

## Conclusion

Safe to ship. The bookings in 3V-Rooms match the catalogue to the minute for 1,916 of 1,917 meetings, and cancelled or online meetings are released in 3V. The block types are mapped correctly. No code change is needed.

Remaining risks:

- **One-off changes that miss 3V,** like the journal club above. They are rare (about 1 in 2,000 meetings), and both sources were wrong or stale in the same way.
- **Free-text rooms:** about 180 meetings have their room only in a note ("siehe Hinweise"). If one of them uses a room we list, and the room was not booked in 3V, we would show it as free. We cannot check this automatically.
- **3V-Rooms is undocumented** and can change. The catalogue OData services above could serve as an independent cross-check in the future.

## Per-room tables (23–27 Nov 2026)

Sample rooms: big lecture halls (KOL-F-101, KOL-G-201 Aula, Y15-G-19, Y15-G-40, BIN-0-K.02), seminar rooms (KOL-G-220, KO2-F-172, KAB-E-03, AND-2-02, AND-3-02), across all three campuses.

| Room | Catalogue events | Matched | Missing in 3V | Only in 3V |
|---|---|---|---|---|
| KOL-F-101 | 21 | 21 | 0 | 4 |
| KOL-G-201 | 8 | 8 | 0 | 6 |
| KOL-G-220 | 24 | 24 | 0 | 0 |
| KO2-F-172 | 20 | 20 | 0 | 1 |
| KAB-E-03 | 19 | 19 | 0 | 1 |
| Y15-G-19 | 8 | 8 | 0 | 1 |
| Y15-G-40 | 17 | 17 | 0 | 1 |
| AND-2-02 | 19 | 19 | 0 | 2 |
| AND-3-02 | 12 | 12 | 0 | 7 |
| BIN-0-K.02 | 17 | 17 | 0 | 0 |
| **Total** | **165** | **165** | **0** | **23** |

<details><summary><b>KOL-F-101</b>: 21 catalogue events, all matched; 4 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 08:00-09:45 | 03VL22AOEC04 Mathematik I (V) (Mathematics I) | 08:00-09:45 | matched |
| Mon 10:15-12:00 | 02VL120PM7-1 Völkerrecht Gruppe 1 | 10:15-12:00 | matched |
| Mon 12:15-13:45 | 03VU22BO0037 Advanced Managerial Accounting (VU) | 12:15-13:45 | matched |
| Mon 14:00-15:45 | 03VU22AOEC05 Finance I (L+E) | 14:00-15:45 | matched |
| Mon 16:15-18:00 | 03VU22BO0037 Advanced Managerial Accounting (VU) | 16:15-18:00 | matched |
| Mon 18:15-20:00 | 07SEZIHP Wissen-schaf(f)t Wissen | 18:15-20:00 | matched |
| Tue 08:00-09:45 | 03UE22AOEC01 Betriebswirtschaftslehre I (Ü) (Business Admi | 08:00-09:45 | matched |
| Tue 12:15-13:45 | 03VU22MO0004 ME4: Accounting and Economics (L+E) | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 02VL120PM6-2 Öffentliches Verfahrensrecht Gruppe 2 | 14:00-15:45 | matched |
| Tue 16:15-18:00 | 03VL22BO0051 Personal Branding and Life Design (L) | 16:15-18:00 | matched |
| Wed 08:00-09:45 | 02VL60PM8-3 Tutorate im Öffentlichen Recht I Gruppe 1 | 08:00-09:45 | matched |
| Wed 10:15-12:00 | 03VL22MO0012 Advanced Microeconomics I (L) | 10:15-12:00 | matched |
| Wed 14:00-15:45 | 03UE22MO0011 Advanced Macroeconomics (E) | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 06VL200-001c Sozialpsychologie (Teil 1) | 16:15-18:00 | matched |
| Thu 08:00-09:45 | 03VL22AOEC01 Betriebswirtschaftslehre I (V) (Business Admi | 08:00-09:45 | matched |
| Thu 10:15-12:00 | 03VL22BO0032 Change Management (L) | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06VL200-002a Forschungsmethoden der Psychologie | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 03VL22AOEC03 Mikroökonomik I (V) (Microeconomics I) | 14:00-15:45 | matched |
| Thu 16:15-18:00 | 03VU22MO0006 Applied Empirical Methods for Business Admini | 16:15-18:00 | matched |
| Fri 10:15-12:00 | 02VL90WM28-1 Sozialversicherungsrecht | 10:15-12:00 | matched |
| Fri 12:15-13:45 | 03VL22AOEC03 Mikroökonomik I (V) (Microeconomics I) | 12:15-13:45 | matched |
| Tue 10:15-12:00 | – | belegt | in 3V only |
| Tue 18:15-21:45 | – | belegt | in 3V only |
| Wed 12:00-14:00 | – | belegt | in 3V only |
| Thu 18:45-20:15 | – | belegt | in 3V only |

</details>

<details><summary><b>KOL-G-201</b>: 8 catalogue events, all matched; 6 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 10:15-12:00 | 02VL60PM7-1 Strafrecht AT I Gruppe 2 | 10:15-12:00 | matched |
| Mon 14:00-15:45 | 03VU22AOEC05 Finance I (L+E) | 14:00-15:45 | matched |
| Tue 10:15-12:00 | 06VL200-301a Arbeits- und Organisationspsychologie 1 | 10:15-12:00 | matched |
| Tue 13:00-13:45 | 06VL440-010a Introduction to English Linguistics, Part I ( | 13:00-13:45 | matched |
| Tue 14:00-15:45 | 06VL440-020a English Literature: Textual Analysis, Part I | 14:00-15:45 | matched |
| Thu 10:15-12:00 | 03VL22AINF01 Informatik und Wirtschaft (V) (Informatics an | 10:15-12:00 | matched |
| Thu 14:00-15:45 | 03VL22AOEC03 Mikroökonomik I (V) (Microeconomics I) | 14:00-15:45 | matched |
| Fri 10:15-12:00 | 06VL200-100a Testtheorie | 10:15-12:00 | matched |
| all week | – | S "Informationen über die Verfügbarkeit auf Anfrage." (11 Mar 2026 – 31 Dec 2030) | in 3V only |
| Mon 16:00-18:00 | – | belegt | in 3V only |
| Mon 18:00-21:45 | – | belegt | in 3V only |
| Tue 18:30-21:30 | – | belegt | in 3V only |
| Wed 17:00-18:45 | – | belegt | in 3V only |
| Fri 18:00-21:00 | – | belegt | in 3V only |

</details>

<details><summary><b>KOL-G-220</b>: 24 catalogue events, all matched; 0 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 10:15-12:00 | 06VL427p001a Arquitectura del cuento en la España del sigl | 10:15-12:00 | matched |
| Mon 12:15-13:45 | 02SE120PS-1 Rechtsphilosophie | 12:15-13:45 | matched |
| Mon 14:00-15:45 | 06SE600-007m Gruppe 13, Proseminar 1: Perlen, Zucker, Porz | 14:00-15:45 | matched |
| Mon 16:15-17:45 | 30SK_D_GB2Vo German B2.2-C2.1: Vokabular und Stilsicherhei | 16:15-17:45 | matched |
| Mon 18:15-19:45 | 30SK_D_GB2Ak German B2.2-C1.1: Akademisches Schreiben | 18:15-19:45 | matched |
| Tue 08:00-09:45 | 30SK_D_ZGA11 German 1: A1.1 | 08:00-09:45 | matched |
| Tue 10:15-11:45 | 30SK-RUS-1 Grundlagen Russisch (Teil I:  A1.2) | 10:15-11:45 | matched |
| Tue 12:15-13:45 | 06KO612p501a Umkämpfte Vergangenheit: Geschichtsnarrative | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 06SE600-515a Kirche und Welt. Spaltungsprozesse im hochmit | 14:00-15:45 | matched |
| Tue 16:15-17:45 | 30SK-F-1 Français B2 (partie I) | 16:15-17:45 | matched |
| Tue 18:00-19:30 | 30SK_F_LaLi Français B2-C1 : Langue et littérature | 18:00-19:30 | matched |
| Wed 08:15-09:45 | 30SK_E_AdvE Advanced English for academic purposes C1-C2 | 08:15-09:45 | matched |
| Wed 10:15-12:00 | 06SE600x410a GGP.S410.2 Grundkurs Geschichte 1, Gruppe 2 | 10:15-12:00 | matched |
| Wed 12:15-13:45 | 06VU620-501a Einführung in die Mediävistik | 12:15-13:45 | matched |
| Wed 14:00-15:45 | 06KO600p012a Datenbanken, Regesten, KI - Innovative Papstt | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 06SE612-502a Biografie und Diktatur | 16:15-18:00 | matched |
| Thu 10:15-12:00 | 06SE682-010a Weltanschauung und Ritual | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06SE200p560a Neuropsychologie in der klinischen Praxis | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 06SE600-512a Konzepte frühmittelalterlicher "Herrschaft" | 14:00-15:45 | matched |
| Thu 16:15-18:00 | 06SE600-001a Film, Radio, Datacenter. Einführung in die Me | 16:15-18:00 | matched |
| Fri 10:15-12:00 | 06SE090E2 Fachdidaktik Englisch II | 10:15-12:00 | matched |
| Fri 12:15-13:45 | 06SK430-012a Română 1 | 12:15-13:45 | matched |
| Fri 14:00-15:45 | 06SK390-013a Techniques de la traduction (B2/C1) | 14:00-15:45 | matched |
| Fri 16:15-18:00 | 06UE390-509a Le génie des lieux | 16:15-18:00 | matched |

</details>

<details><summary><b>KO2-F-172</b>: 20 catalogue events, all matched; 1 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 10:15-12:00 | 06SE600-005a Rom und die Germanen – Freunde oder Feinde? Z | 10:15-12:00 | matched |
| Mon 14:00-15:45 | 06SE600-001b Weltbilder im Web. Digitalisierte mittelalter | 14:00-15:45 | matched |
| Mon 16:15-18:00 | 06SE600-015a Sister Republics: Digital History und die Sch | 16:15-18:00 | matched |
| Tue 10:15-12:00 | 06SE600-514b Die unauflösliche Ehe als mittelalterliches G | 10:15-12:00 | matched |
| Tue 12:15-13:45 | 06SE160p502a Conservatism | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 06SE600-011a Arbeit im Mittelalter | 14:00-15:45 | matched |
| Tue 16:15-17:45 | 03SEDOEC1271 Doktoranden- und Forschungsseminar (S) HS26 ( | 16:15-17:45 | matched |
| Wed 08:00-09:45 | 06SE600x410a GGP.S410.4 Grundkurs Geschichte 1, Gruppe 4 | 08:00-09:45 | matched |
| Wed 10:15-12:00 | 06SE600-007k Gruppe 11, Proseminar 1: The Making of Us and | 10:15-12:00 | matched |
| Wed 12:15-13:45 | 06SE360-118a Mystik und (weibliche) Autorschaft im Mittela | 12:15-13:45 | matched |
| Wed 14:00-15:45 | 06SE600-006a Blutige Unterhaltung? Die Schauspiele im anti | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 06SE600-601a Slavery and Industrialization | 16:15-18:00 | matched |
| Thu 10:15-12:00 | 06SE600-003a Irdische Fremde & Himmlische Heimat? - Politi | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06SE600-016a Queere Schweiz. Eine Archivgeschichte | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 06SE600-018a Politisierter Aussenraum: Medien und Öffentli | 14:00-15:45 | matched |
| Thu 16:15-18:00 | 06SE600-014a Das Reich der Merowinger - Politik und Gesell | 16:15-18:00 | matched |
| Fri 08:00-09:45 | 02SE120PS-1 Privatautonomie im Schuldrecht - Grenzen der | 08:00-09:45 | matched |
| Fri 10:15-12:00 | 06SE240p003a Narrative Identität: Brückenschlag zwischen T | 10:15-12:00 | matched |
| Fri 12:15-13:45 | 01SK91L0039 Ugaritisch I: Sprache und Schriftkultur | 12:15-13:45 | matched |
| Fri 14:00-15:45 | 01UE14LB000 BW: Bibelkunde AT | 14:00-15:45 | matched |
| Tue 18:15-21:45 | – | belegt | in 3V only |

</details>

<details><summary><b>KAB-E-03</b>: 19 catalogue events, all matched; 1 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 08:00-09:45 | 06SE090Rel3 Fachdidaktik Religion III: Einsatz von Spielf | 08:00-09:45 | matched |
| Mon 10:15-12:00 | 06SE090Geo1a Fachdidaktik Geographie I (Rafflenbeul) | 10:15-12:00 | matched |
| Mon 12:15-13:45 | 06UE226-012b Wirtschaftspädagogik: Lernen in der kaufmänni | 12:15-13:45 | matched |
| Mon 14:00-15:45 | 01SE56LP012 RKE.S420.1 Religionskulturen in Geschichte un | 14:00-15:45 | matched |
| Mon 16:15-18:00 | 01SE56LP012 RKE.S420.2 Religionskulturen in Geschichte un | 16:15-18:00 | matched |
| Tue 08:00-09:45 | 06SE222Vic Videos in the Classroom: How to Use and Desig | 08:00-09:45 | matched |
| Tue 10:15-12:00 | 06SE222HD Herausforderungen der Digitalisierung bewälti | 10:15-12:00 | matched |
| Tue 12:15-13:45 | 06SE226-015e Qualitative Methoden: Schwerpunkt Projektkonz | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 06SE222KiK Kommunikation im Klassenzimmer: Zentrale Unte | 14:00-15:45 | matched |
| Tue 16:15-18:00 | 06UE226-007e Bildungsprozesse und Schule, Gruppe E | 16:15-18:00 | matched |
| Wed 08:00-09:45 | 06SE222EMSE Emotionale, motivationale und soziale Effekte | 08:00-09:45 | matched |
| Wed 12:15-13:45 | 06UE226-020e Sozialpädagogik - Bildung und Erziehung im Ko | 12:15-13:45 | matched |
| Thu 08:00-09:45 | 06SE090LLB1S Unterrichten an der Berufsfachschule: Hospita | 08:00-09:45 | matched |
| Thu 10:15-12:00 | 06SE440x452a EN.S452.1 Specialisation English Literature, | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06SE222ALL Aufgabenkultur und komplexe Lehr- Lernumgebun | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 06SE160p014a Social Contract Theory | 14:00-15:45 | matched |
| Thu 16:15-18:00 | 06UE226-001g Einführung in wissenschaftliches Arbeiten, Gr | 16:15-18:00 | matched |
| Fri 08:00-09:45 | 06SE090Ge2 Fachdidaktik Geschichte II | 08:00-09:45 | matched |
| Fri 10:15-12:00 | 06SE090Ge3 Fachdidaktik Geschichte III | 10:15-12:00 | matched |
| Wed 10:15-12:00 | – | belegt | in 3V only |

</details>

<details><summary><b>Y15-G-19</b>: 8 catalogue events, all matched; 1 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 13:00-15:00 | 05VL.152_tet Tierschutz, Ethologie, Tierhaltung | 13:00-15:00 | matched |
| Tue 13:00-14:00 | 05VL.125_ra Strahlenphysik, Radiologie | 13:00-14:00 | matched |
| Tue 14:00-15:00 | 05VL.130_kt Klinische Tätigkeit, Einführung | 14:00-15:00 | matched |
| Wed 11:00-13:00 | 05VL.151_kg Klassische Genetik, Tierzucht | 11:00-13:00 | matched |
| Thu 08:00-10:00 | 05VL.151_kg Klassische Genetik, Tierzucht | 08:00-10:00 | matched |
| Fri 08:00-10:00 | 05VL.121_bw Bewegungsapparat | 08:00-10:00 | matched |
| Fri 10:00-12:00 | 05VL.121_bw Bewegungsapparat | 10:00-12:00 | matched |
| Fri 15:00-17:00 | 05VL.126_zm Molekularbiologie I | 15:00-17:00 | matched |
| Thu 16:15-17:30 | – | belegt | in 3V only |

</details>

<details><summary><b>Y15-G-40</b>: 17 catalogue events, all matched; 1 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 08:00-09:45 | 07VLBIO344 Development of the Nervous System | 08:00-09:45 | matched |
| Mon 10:15-12:00 | 07VLMAT141.1 Lineare Algebra für die Naturwissenschaften | 10:15-12:00 | matched |
| Mon 12:15-13:45 | 07VLGEO111.1 Physische Geographie I: Grundzüge und Sphären | 12:15-13:45 | matched |
| Mon 14:00-15:45 | 02VL90WM19-1 Kriminaltechnik - Naturwissenschaftliche Fore | 14:00-15:45 | matched |
| Tue 08:00-09:45 | 07VUBME235 Physiologie und Anatomie I | 08:00-09:45 | matched |
| Tue 10:15-12:00 | 07VLCHE154.1 Physikalische Chemie I für die Life Sciences | 10:15-12:00 | matched |
| Tue 16:15-18:00 | 06VL615-018a Einführung Methoden und Statistik | 16:15-18:00 | matched |
| Wed 08:00-09:45 | 07VLGEO113.1 Fernerkundung und Geographische Informationsw | 08:00-09:45 | matched |
| Wed 10:15-12:00 | 07VLGEO231.1 Phyische Geographie III: Geomorphologie und G | 10:15-12:00 | matched |
| Wed 12:15-14:00 | 07UEMAT182.2 Übungen Analysis für die Naturwissenschaften, | 12:15-14:00 | matched |
| Wed 14:00-15:45 | 07VLGEO233.1 Fernerkundung & Geographische Informationswis | 14:00-15:45 | matched |
| Thu 08:00-09:45 | 07VLBME236 Biomedicine I | 08:00-09:45 | matched |
| Thu 10:15-12:00 | 07VLGEO112.1 Humangeographie I: Geographien der Globalisie | 10:15-12:00 | matched |
| Thu 12:15-14:00 | 07UEMAT182.2 Übungen Analysis für die Naturwissenschaften | 12:15-14:00 | matched |
| Fri 08:00-09:45 | 07VLBIO112.1 Zellbiologie Vorlesung | 08:00-09:45 | matched |
| Fri 10:15-12:00 | 07VLBIO113.1 Evolution und Biodiversität I | 10:15-12:00 | matched |
| Fri 13:00-14:45 | 07VLMAT121.1 Analysis I | 13:00-14:45 | matched |
| Tue 14:00-16:00 | – | belegt | in 3V only |

</details>

<details><summary><b>AND-2-02</b>: 19 catalogue events, all matched; 2 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 10:15-12:00 | 06UE721-002a Einführung in die Arbeit mit ethnologischen T | 10:15-12:00 | matched |
| Mon 12:15-13:45 | 06SE615-501b Preparation for the Research Seminar: The Pol | 12:15-13:45 | matched |
| Mon 14:00-15:45 | 06SE721-500a Current Debates in Social Anthropology | 14:00-15:45 | matched |
| Tue 08:00-09:45 | 06SE721p130a Kaste in Indien | 08:00-09:45 | matched |
| Tue 10:15-12:00 | 06SE240p027a Sociological Imagination and Other Classic Te | 10:15-12:00 | matched |
| Tue 12:15-13:45 | 06SE272-504a Conditions of Language Dynamics | 12:15-13:45 | matched |
| Tue 16:15-18:00 | 06TT240-006b Statistik I, Tutorat, Gruppe 02 | 16:15-18:00 | matched |
| Wed 08:00-09:45 | 06UE721-002b Einführung in die Arbeit mit ethnologischen T | 08:00-09:45 | matched |
| Wed 12:15-13:45 | 06SE721p335a Urban Anthropology | 12:15-13:45 | matched |
| Wed 14:00-15:45 | 06UE254-011e Wissenschaftliches Arbeiten, Übung Gruppe 5 | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 06KO721-560b Research Module 1/Research Module 4 Global Fu | 16:15-18:00 | matched |
| Thu 10:15-12:00 | 06SE615p008a Spezialisierung: Direkte Demokratie Schweiz i | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06SE721p250a Anthropology of Australia: Indigenous Worlds | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 06KO200-902x (G37) Forschungskolloquium "Differentielle Ps | 14:00-15:45 | matched |
| Thu 16:15-18:00 | 06SE721p129a Körper, Sinne und Schönheit: Verkörperte Mach | 16:15-18:00 | matched |
| Fri 08:00-09:45 | 06SE200-003q Interaktives Proseminar Gruppe 17 | 08:00-09:45 | matched |
| Fri 10:15-12:00 | 06SE200-003i Interaktives Proseminar Gruppe 09 | 10:15-12:00 | matched |
| Fri 12:15-13:45 | 06SE200-003n Interaktives Proseminar Gruppe 14 | 12:15-13:45 | matched |
| Fri 14:00-15:45 | 06SE200p704a Visual Perception | 14:00-15:45 | matched |
| Wed 10:15-12:00 | – | belegt | in 3V only |
| Fri 16:00-18:00 | – | belegt | in 3V only |

</details>

<details><summary><b>AND-3-02</b>: 12 catalogue events, all matched; 7 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 10:15-12:00 | 06SE200p705m Intervention development and adaptation to ad | 10:15-12:00 | matched |
| Tue 10:15-12:00 | 06SE200p800x Eine Einführung in die Längsschnittdatenanaly | 10:15-12:00 | matched |
| Tue 12:15-13:45 | 06TT523-004a Machine Learning for Language Technology (Tut | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 06SE240p010a Exemplary Studies of Political Sociology and | 14:00-15:45 | matched |
| Wed 08:00-12:00 | 06SE240p022a The Everyday in Theory: An Introduction to Et | 08:00-12:00 | matched |
| Wed 14:00-15:45 | 06SE722p003a Anthropology of Disaster / Anthropocene and N | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 06SE240p007a Kausalanalyse in den Sozialwissenschaften | 16:15-18:00 | matched |
| Thu 08:00-09:45 | 06SE240p014a The Labour Market: Occupational Structures, C | 08:00-09:45 | matched |
| Thu 10:15-12:00 | 06UE721p411a Narrating Otherwise: Graphic Anthropology, Et | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06VU523p004a Focus course Text Technology: Model Analysis | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 06SE200p705b Urteilen und Entscheiden über die Lebensspann | 14:00-15:45 | matched |
| Fri 10:15-12:00 | 06SE523p501a Reinforcement Learning for Language Modeling | 10:15-12:00 | matched |
| Mon 08:00-09:45 | – | belegt | in 3V only |
| Mon 14:00-15:45 | – | belegt | in 3V only |
| Mon 16:15-18:00 | – | belegt | in 3V only |
| Tue 08:00-09:45 | – | belegt | in 3V only |
| Wed 12:15-13:45 | – | belegt | in 3V only |
| Fri 12:15-13:45 | – | belegt | in 3V only |
| Fri 14:00-15:45 | – | belegt | in 3V only |

</details>

<details><summary><b>BIN-0-K.02</b>: 17 catalogue events, all matched; 0 extra 3V blocks</summary>

| When | Catalogue course | 3V-Rooms block | Result |
|---|---|---|---|
| Mon 08:00-12:00 | 03VU22MI0019 Network Science (L+E) | 08:00-12:00 | matched |
| Mon 12:15-13:45 | 03VU22AINF04 People-Oriented Computing (L+E) | 12:15-13:45 | matched |
| Mon 14:00-18:00 | 03VL22MI0005 Foundations of Data Science (L+E) | 14:00-18:00 | matched |
| Tue 08:00-09:45 | 03VU22BI0003 Numerical Methods in Informatics (L+E) | 08:00-09:45 | matched |
| Tue 10:15-12:00 | 06VL721-001a Einführung in die Ethnologie | 10:15-12:00 | matched |
| Tue 12:15-13:45 | 06VL200-502a Gesundheitspsychologie | 12:15-13:45 | matched |
| Tue 14:00-15:45 | 06VL721-101a Kernbereich Verwandtschaft und Gender | 14:00-15:45 | matched |
| Tue 16:15-18:00 | 03VU22MI0043 Reinforcement Learning (L+E) | 16:15-18:00 | matched |
| Wed 08:00-09:45 | 06VL240-001a Grundlagen der Soziologie | 08:00-09:45 | matched |
| Wed 10:15-13:45 | 03VU22BI0004 Software Construction (L+E) | 10:15-13:45 | matched |
| Wed 14:00-15:45 | 06VL721-102a Kernbereich Ökologie und Wirtschaft | 14:00-15:45 | matched |
| Wed 16:15-18:00 | 03VL22MI0038 Human-Centered AI | 16:15-18:00 | matched |
| Thu 08:00-09:45 | 03VL22MI0038 Human-Centered AI | 08:00-09:45 | matched |
| Thu 10:15-12:00 | 06VL721-001a Einführung in die Ethnologie | 10:15-12:00 | matched |
| Thu 12:15-13:45 | 06VL240-006a Statistik I | 12:15-13:45 | matched |
| Thu 14:00-15:45 | 03VU22BI0003 Numerical Methods in Informatics (L+E) | 14:00-15:45 | matched |
| Fri 09:00-18:00 | 06SE200p800f Lineare Strukturgleichungsmodelle - von einfa | 09:00-18:00 | matched |

</details>

