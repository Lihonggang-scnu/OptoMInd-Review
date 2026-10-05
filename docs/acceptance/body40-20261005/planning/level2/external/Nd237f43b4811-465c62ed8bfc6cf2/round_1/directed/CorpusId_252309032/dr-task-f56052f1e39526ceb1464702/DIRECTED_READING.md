## Q01

本研究提出并验证了一个由肌苷调控的UBA6依赖性通路，该通路控制肿瘤的内在免疫原性，进而影响免疫检查点阻断（ICB）的敏感性。核心机制在于：肌苷作为一种嘌呤代谢物，能够直接进入肿瘤细胞（通过ENT转运体而非腺苷受体），并直接结合和抑制泛素激活酶UBA6的活性。UBA6不仅激活泛素，还激活泛素样蛋白FAT10。肌苷通过干扰UBA6与E2酶USE1的结合，抑制了UBA6介导的FAT10硫酯键形成。UBA6活性的降低导致肿瘤细胞内炎症信号通路（如TNF-α、IFN-γ响应基因）和抗原加工/呈递相关基因的上调，从而增加MHC-I表达和肿瘤免疫原性。这种增强的免疫原性使肿瘤细胞对CD8+ T细胞介导的细胞毒性更加敏感，从而克服肿瘤内在的ICB耐药性。临床数据显示，患者血浆中高水平的肌苷以及肿瘤组织中低水平的UBA6表达均与接受ICB治疗后的良好预后（更长的OS/PFS和更高的客观缓解率）相关。

### Concrete examples

- **本研究:** Use: 提供肌苷抑制UBA6的具体分子机制证据，包括结合筛选、生化验证及功能回补实验，证明肌苷通过直接靶向肿瘤细胞内的UBA6发挥作用。. 通过LiP-SMap化学蛋白质组学筛选，在4T1细胞裂解物中鉴定出23个与肌苷结合的候选蛋白，其中仅UBA6的缺失能显著增强T细胞介导的肿瘤杀伤。体外生化实验证实，肌苷直接减少UBA6与其特异性E2酶USE1之间的相互作用，并降低UBA6介导的FAT10硫酯键形成活性。此外，删除UBA6的泛素折叠结构域（UFD）可消除肌苷对UBA6-USE1相互作用的抑制作用，且UBA6缺失消除了肌苷对T细胞介导肿瘤杀伤的增敏效应，表明肌苷通过直接抑制UBA6活性来增强肿瘤免疫原性。 Conditions: 使用4T1细胞裂解液进行LiP-SMap筛选；使用HEK293细胞和纯化蛋白进行体外相互作用及硫酯活性测定；使用CRISPR敲除UBA6的4T1和B16-GMCSF肿瘤细胞系进行功能验证。
- **本研究:** Use: 提供血浆肌苷水平与接受ICB治疗患者生存期关联的临床队列数据，确立肌苷作为ICB疗效生物标志物的临床相关性。. 在CheckMate 025肾细胞癌（RCC）患者队列中，接受纳武利尤单抗（抗PD-1）治疗的患者的血浆肌苷水平与总生存期（OS）呈正相关：高肌苷组中位OS为33个月，低肌苷组为22个月；而在接受依维莫司（mTOR抑制剂）治疗的患者中无此关联。这表明高水平肌苷能为接受ICB治疗的患者带来持久的生存获益。 Conditions: 分析CheckMate 025临床试验中394名接受纳武利尤单抗治疗和349名接受依维莫司治疗的RCC患者的血浆代谢谱；以代谢物中位数分组比较OS。 [R15]
- **本研究:** Use: 补充说明肌苷在肿瘤微环境（TME）中的浓度缺口或来源限制，指出虽然肌苷由肠道菌群产生并可进入循环，但其在TME局部的具体浓度动态及测量受限于腺苷的快速降解，暗示TME内有效肌苷浓度的维持可能存在挑战。. 讨论部分指出，尽管肌苷是腺苷的代谢产物且循环水平受饮食、基因和药物影响，但腺苷在体内可迅速降解为肌苷（约10秒）并在血浆中快速清除（约30秒），而肌苷的生物半衰期较长（约15小时）。由于样本缺失，研究未能在其临床队列中验证肌苷测量或转录组分析。这提示在评估TME中肌苷的实际有效浓度时，需考虑其前体腺苷的不稳定性以及局部摄取机制（如ENT转运体）的作用，目前TME内肌苷的具体浓度缺口及其对UBA6抑制效率的影响尚待进一步解析。 Conditions: 基于文献综述和讨论部分的逻辑推断，结合研究中提到的腺苷半衰期短、肌苷半衰期长的药代动力学特征，以及未能直接在患者肿瘤组织中测量肌苷的事实。 [R55, R56]

Open points: TME中肌苷的具体局部浓度及其动态变化尚未量化，特别是考虑到腺苷到肌苷的快速转化及清除过程。; 肌苷抑制UBA6后，下游具体的泛素化/FAT10化底物蛋白及其如何具体调控MHC-I上调的详细分子路径仍需进一步阐明。; 不同癌症类型中UBA6表达基线差异对肌苷疗效预测价值的普适性需更大规模队列验证。

## Optional bibliography

- [R1] 1.Abril-RodriguezGRibasASnapShot: immune checkpoint inhibitorsCancer Cell201731848848 e84110.1016/j.ccell.2017.05.01028609660
- [R2] 2.KalbasiARibasATumour-intrinsic resistance to immune checkpoint blockadeNat. Rev. Immunol.202020253910.1038/s41577-019-0218-431570880PMC8499690
- [R3] 3.PittJMResistance mechanisms to immune-checkpoint blockade in cancer: tumor-intrinsic and -extrinsic factorsImmunity2016441255126910.1016/j.immuni.2016.06.00127332730
- [R4] 4.TanoueTA defined commensal consortium elicit CD8 T cells and anti-cancer immunityNature201956560060510.1038/s41586-019-0878-z30675064
- [R5] 5.GopalakrishnanVGut microbiome modulates response to anti-PD-1 immunotherapy in melanoma patientsScience20183599710310.1126/science.aan423629097493PMC5827966
- [R6] 6.MatsonVThe commensal microbiome is associated with anti-PD-1 efficacy in metastatic melanoma patientsScience201835910410810.1126/science.aao329029302014PMC6707353
- [R7] 7.RoutyBGut microbiome influences efficacy of PD-1-based immunotherapy against epithelial tumorsScience2018359919710.1126/science.aan370629097494
- [R8] 8.SivanACommensal Bifidobacterium promotes antitumor immunity and facilitates anti-PD-L1 efficacyScience20153501084108910.1126/science.aac425526541606PMC4873287
- [R9] 9.VetizouMAnticancer immunotherapy by CTLA-4 blockade relies on the gut microbiotaScience20153501079108410.1126/science.aad132926541610PMC4721659
- [R10] 10.PinatoDJAssociation of prior antibiotic treatment with survival and response to immune checkpoint inhibitor therapy in patients with cancerJAMA Oncol.201951774177810.1001/jamaoncol.2019.278531513236PMC6743060
- [R11] 11.RooksMGGarrettWSGut microbiota, metabolites and host immunityNat. Rev. Immunol.20161634135210.1038/nri.2016.4227231050PMC5541232
- [R12] 12.CampbellCBacterial metabolism of bile acids promotes generation of peripheral regulatory T cellsNature202058147547910.1038/s41586-020-2193-032461639PMC7540721
- [R13] 13.Ma, C. et al. Gut microbiome-mediated bile acid metabolism regulates liver cancer via NKT cells. Science36010.1126/science.aan5931 (2018).10.1126/science.aan5931PMC640788529798856
- [R14] 14.DePeaux, K. & Delgoffe, G. M. Metabolic barriers to cancer immunotherapy. Nat. Rev. Immunol.10.1038/s41577-021-00541-y (2021).10.1038/s41577-021-00541-yPMC855380033927375
- [R15] 15.LiHMetabolomic adaptations and correlates of survival to immune checkpoint blockadeNat. Commun.201910434610.1038/s41467-019-12361-931554815PMC6761178
- [R16] 16.ZhangBB cell-derived GABA elicits IL-10(+) macrophages to limit anti-tumour immunityNature202159947147610.1038/s41586-021-04082-134732892PMC8599023
- [R17] 17.LooTMGut microbiota promotes obesity-associated liver cancer through PGE2-mediated suppression of antitumor ImmunityCancer Discov.2017752253810.1158/2159-8290.CD-16-093228202625
- [R18] 18.LevyMBlacherEElinavEMicrobiome, metabolites and host immunityCurr. Opin. Microbiol.20173581510.1016/j.mib.2016.10.00327883933
- [R19] 19.QinHChenYLipid metabolism and tumor antigen presentationAdv. Exp. Med. Biol.2021131616918910.1007/978-981-33-6785-2_1133740250
- [R20] 20.BrestoffJRArtisDImmune regulation of metabolic homeostasis in health and diseaseCell201516114616010.1016/j.cell.2015.02.02225815992PMC4400287
- [R21] 21.PavlovaNNThompsonCBThe emerging hallmarks of cancer metabolismCell Metab.201623274710.1016/j.cmet.2015.12.00626771115PMC4715268
- [R22] 22.Leone, R. D. et al. Glutamine blockade induces divergent metabolic programs to overcome tumor immune evasion. Science, eaav2588, (2019).10.1126/science.aav2588PMC702346131699883
- [R23] 23.Mendez-SalazarEOMartinez-NavaGAUric acid extrarenal excretion: the gut microbiome as an evident yet understated factor in gout developmentRheumatol. Int.20214240341210.1007/s00296-021-05007-x34586473
- [R24] 24.Chiaro, T. R. et al. A member of the gut mycobiota modulates host purine metabolism exacerbating colitis in mice. Science Transl. Med.9, 10.1126/scitranslmed.aaf9044 (2017).10.1126/scitranslmed.aaf9044PMC599491928275154
- [R25] 25.Mager, L. F. et al. Microbiome-derived inosine modulates response to checkpoint inhibitor immunotherapy. Science, eabc3421, 1481–1489 (2020).10.1126/science.abc342132792462
- [R26] 26.MotzerRJNivolumab versus everolimus in advanced renal-cell carcinomaN. Engl. J. Med.20153731803181310.1056/NEJMoa151066526406148PMC5719487
- [R27] 27.HaskoGSitkovskyMVSzaboCImmunomodulatory and neuroprotective effects of inosineTrends Pharmacol. Sci.20042515215710.1016/j.tips.2004.01.00615019271
- [R28] 28.De HenauOOvercoming resistance to checkpoint blockade therapy by targeting PI3Kgamma in myeloid cellsNature201653944344710.1038/nature2055427828943PMC5634331
- [R29] 29.AranDReference-based analysis of lung single-cell sequencing reveals a transitional profibrotic macrophageNat. Immunol.20192016317210.1038/s41590-018-0276-y30643263PMC6340744
- [R30] 30.HaskoGInosine inhibits inflammatory cytokine production by a posttranscriptional mechanism and protects against endotoxin-induced shockJ. Immunol.20001641013101910.4049/jimmunol.164.2.101310623851
- [R31] 31.PiazzaIA map of protein-metabolite interactions reveals principles of chemical communicationCell2018172358372 e32310.1016/j.cell.2017.12.00629307493
- [R32] 32.FuJLarge-scale public data reuse to model immunotherapy response and resistanceGenome Med.2020122110.1186/s13073-020-0721-z32102694PMC7045518
- [R33] 33.PanDA major chromatin regulator determines resistance of tumor cells to T cell-mediated killingScience201835977077510.1126/science.aao171029301958PMC5953516
- [R34] 34.Kearney, C. J. et al. Tumor immune evasion arises through loss of TNF sensitivity. Sci. Immunol.3, 10.1126/sciimmunol.aar3451 (2018).10.1126/sciimmunol.aar345129776993
- [R35] 35.GroettrupMPelzerCSchmidtkeGHofmannKActivating the ubiquitin family: UBA6 challenges the fieldTrends Biochem. Sci.20083323023710.1016/j.tibs.2008.01.00518353650
- [R36] 36.ChiuYHSunQChenZJE1-L2 activates both ubiquitin and FAT10Mol. Cell2007271014102310.1016/j.molcel.2007.08.02017889673
- [R37] 37.GavinJMMechanistic studies on activation of ubiquitin and di-ubiquitin-like protein, FAT10, by ubiquitin-like modifier activating enzyme 6, Uba6J. Biol. Chem.2012287155121552210.1074/jbc.M111.33619822427669PMC3346097
- [R38] 38.BaslerMBuergerSGroettrupMThe ubiquitin-like modifier FAT10 in antigen processing and antimicrobial defenseMol. Immunol.20156812913210.1016/j.molimm.2015.04.01225983082
- [R39] 39.EbsteinFLehmannAKloetzelPMThe FAT10- and ubiquitin-dependent degradation machineries exhibit common and distinct requirements for MHC class I antigen presentationCell. Mol. Life Sci.2012692443245410.1007/s00018-012-0933-522349260PMC3383951
- [R40] 40.HyerMLA small-molecule inhibitor of the ubiquitin activating enzyme for cancer treatmentNat. Med.20182418619310.1038/nm.447429334375
- [R41] 41.HeBResetting microbiota by Lactobacillus reuteri inhibits T reg deficiency-induced autoimmunity via adenosine A2A receptorsJ. Exp. Med.201721410712310.1084/jem.2016096127994068PMC5206500
- [R42] 42.WelihindaAAKaurMGreeneKZhaiYAmentoEPThe adenosine metabolite inosine is a functional agonist of the adenosine A2A receptor with a unique signaling biasCell. Signal.20162855256010.1016/j.cellsig.2016.02.01026903141PMC4826793
- [R43] 43.JiangPSignatures of T cell dysfunction and exclusion predict cancer immunotherapy responseNat. Med.2018241550155810.1038/s41591-018-0136-130127393PMC6487502
- [R44] 44.RiazNTumor and microenvironment evolution during immunotherapy with nivolumabCell2017171934949 e91610.1016/j.cell.2017.09.02829033130PMC5685550
- [R45] 45.Van AllenEMGenomic correlates of response to CTLA-4 blockade in metastatic melanomaScience201535020721110.1126/science.aad009526359337PMC5054517
- [R46] 46.GideTNDistinct immune cell populations define response to anti-PD-1 monotherapy and anti-PD-1/anti-CTLA-4 combined therapyCancer Cell201935238255 e23610.1016/j.ccell.2019.01.00330753825
- [R47] 47.WangTInosine is an alternative carbon source for CD8(+)-T-cell function under glucose restrictionNat. Metab.2020263564710.1038/s42255-020-0219-432694789PMC7371628
- [R48] 48.JiangZHsuJLLiYHortobagyiGNHungMCCancer cell metabolism bolsters immunotherapy resistance by promoting an immunosuppressive tumor microenvironmentFront. Oncol.202010119710.3389/fonc.2020.0119732775303PMC7387712
- [R49] 49.FujisakaSDiet, genetics, and the gut microbiome drive dynamic changes in plasma metabolitesCell Rep.2018223072308610.1016/j.celrep.2018.02.06029539432PMC5880543
- [R50] 50.PanebiancoCInfluence of gemcitabine chemotherapy on the microbiota of pancreatic cancer xenografted miceCancer Chemother. Pharmacol.20188177378210.1007/s00280-018-3549-029473096
- [R51] 51.BarcenaCHealthspan and lifespan extension by fecal microbiota transplantation into progeroid miceNat. Med.2019251234124210.1038/s41591-019-0504-531332389
- [R52] 52.ChenJInosine released from dying or dead cells stimulates cell proliferation via adenosine receptorsFront. Immunol.2017850410.3389/fimmu.2017.0050428496447PMC5406388
- [R53] 53.Liu, X. et al. The non-canonical ubiquitin activating enzyme UBA6 suppresses epithelial-mesenchymal transition of mammary epithelial cells. 8, 87480–87493 (2017).10.18632/oncotarget.20900PMC567564829152096
- [R54] 54.TokheimCSystematic characterization of mutations altering protein degradation in human cancersMol. Cell20218112921308 e121110.1016/j.molcel.2021.01.02033567269PMC9245451
- [R55] 55.MöserGHSchraderJDeussenATurnover of adenosine in plasma of human and dog bloodAm. J. Physiol.1989256C799C80610.1152/ajpcell.1989.256.4.C7992539728
- [R56] 56.ViegasTXOmuraGAStoltzRRKisickiJPharmacokinetics and pharmacodynamics of peldesine (BCX-34), a purine nucleoside phosphorylase inhibitor, following single and multiple oral doses in healthy volunteersJ. Clin. Pharmacol.20004041042010.1177/0091270002200899110761169
- [R57] 57.KumarRYonedaJFidlerIJDongZGM-CSF-transduced B16 melanoma cells are highly susceptible to lysis by normal murine macrophages and poorly tumorigenic in immune-compromised miceJ. Leukoc. Biol.19996510210810.1002/jlb.65.1.1029886252
- [R58] 58.LiberzonAThe Molecular Signatures Database (MSigDB) hallmark gene set collectionCell Syst.2015141742510.1016/j.cels.2015.12.00426771021PMC4707969
- [R59] 59.KramerAGreenJPollardJJrTugendreichSCausal analysis approaches in ingenuity pathway analysisBioinformatics20143052353010.1093/bioinformatics/btt70324336805PMC3928520
- [R60] 60.WangXIn vivo CRISPR screens identify the E3 ligase Cop1 as a modulator of macrophage infiltration and cancer immunotherapy targetCell202118453575374.e2210.1016/j.cell.2021.09.00634582788PMC9136996
- [R61] 61.HeBAntibiotic-modulated microbiome suppresses lethal inflammation and prolongs lifespan in Treg-deficient miceMicrobiome2019714510.1186/s40168-019-0751-131699146PMC6839243
- [R62] 62.HuangBMucosal profiling of pediatric-onset colitis and IBD reveals common pathogenics and therapeutic pathwaysCell201917911601176 e112410.1016/j.cell.2019.10.02731730855
- [R63] 63.IshizukaJJLoss of ADAR1 in tumours overcomes resistance to immune checkpoint blockadeNature2019565434810.1038/s41586-018-0768-930559380PMC7241251
- [R64] 64.TangZKangBLiCChenTZhangZGEPIA2: an enhanced web server for large-scale expression profiling and interactive analysisNucleic Acids Res.201947W556W56010.1093/nar/gkz43031114875PMC6602440
- [R65] 65.NishinoMDeveloping a common language for tumor response to immunotherapy: immune-related response criteria using unidimensional measurementsClin. Cancer Res.2013193936394310.1158/1078-0432.CCR-13-089523743568PMC3740724
- [R66] 66.HuangYXieJDingYZhouXExtranodal natural killer/T-cell lymphoma in children and adolescents: a report of 17 cases in ChinaAm. J. Clin. Pathol.2016145465410.1093/ajcp/aqv01026712870