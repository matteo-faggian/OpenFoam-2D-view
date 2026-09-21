# OpenFOAM 2D View

Interfaccia web (Flask) per impostare, meshare ed eseguire casi **OpenFOAM** su Windows
senza toccare un solo dizionario a mano.

Disegni la geometria in un canvas 2D, scegli solver, modello di turbolenza, proprieta'
del fluido e condizioni al contorno da un pannello, premi *Mesh* e *Simula*: l'app
genera la mesh con gmsh, scrive il case OpenFOAM completo, lancia il solver (anche in
parallelo con MS-MPI), mostra il log in streaming e il grafico dei residui, e apre il
risultato in ParaView.

> Progetto personale, nato per non dover riscrivere ogni volta `blockMeshDict`,
> `fvSchemes`, `fvSolution` & co. per casi 2D di aerodinamica.

---

## Funzionalita'

- **Editor geometria 2D** su canvas (Fabric.js): profili, ostacoli, dominio.
- **Mesh 2D** con [gmsh](https://gmsh.info/) + conversione `gmshToFoam` e correzione
  automatica dei tipi di patch (`empty` su frontAndBack, `wall`, `inlet`, `outlet`...).
- **Mesh 3D** opzionale: caricamento di un `.stl` e pipeline
  `blockMesh` -> `surfaceFeatures` -> `snappyHexMesh` -> `checkMesh`.
- **Scrittura automatica del case**: `controlDict`, `fvSchemes`, `fvSolution`,
  `thermophysicalProperties`, `turbulenceProperties`, condizioni al contorno,
  `decomposeParDict`.
- **Solver supportati**: `rhoSimpleFoam`, `rhoPimpleFoam`, `rhoCentralFoam`,
  `sonicFoam`, `simpleFoam`.
- **Turbolenza**: kOmegaSST, kEpsilon, Spalart-Allmaras, laminare.
- **Esecuzione parallela** con `decomposePar` + `mpiexec` (MS-MPI).
- **Log del solver in streaming** (SSE) e **grafico dei residui** parsati dal log.
- **Helper animazioni**: calcola il `writeInterval` giusto per ottenere un video
  ParaView di durata e frame rate desiderati.
- **Avvio di ParaView** con un click sul case corrente.

---

## Requisiti

| Componente | Versione | Note |
|---|---|---|
| Windows | 10/11 x64 | l'app usa l'ambiente MSYS2 di OpenFOAM |
| [OpenFOAM](https://www.openfoam.com/) | v2312 (build Windows/MSYS2) | |
| Python | 3.10+ | con `pip` nel PATH |
| MS-MPI | qualsiasi | solo per l'esecuzione parallela |
| ParaView | opzionale | per la post-elaborazione |

Pacchetti Python (installati automaticamente da `start.bat`):

```
flask>=3.0
gmsh>=4.0
matplotlib>=3.5
numpy>=1.20
```

---

## Installazione e avvio

```bash
git clone https://github.com/matteo-faggian/OpenFoam-2D-view.git
cd OpenFoam-2D-view
pip install -r requirements.txt
python app.py
```

Poi apri <http://127.0.0.1:5000>.

Su Windows puoi anche fare **doppio click su `start.bat`**: controlla Python, i
pacchetti, l'installazione di OpenFOAM, MS-MPI e la `libPstream.dll` corretta
(versione MS-MPI e non quella dummy), poi avvia il server.

### Configurazione dei percorsi

I percorsi dell'installazione OpenFOAM sono in testa a `of_runner.py` e vanno
adattati alla tua macchina:

```python
MSYS_BASH   = r"I:\OPENFOAM\v2312\msys64\usr\bin\bash.exe"
OF_BASHRC   = "/home/ofuser/OpenFOAM/OpenFOAM-v2312/etc/bashrc"
OF_BIN      = r"I:\OPENFOAM\v2312\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2312\platforms\win64MingwDPInt32Opt\bin"
MPIEXEC_MSYS = "/i/OPENFOAM/bin/mpiexec.exe"
```

---

## Flusso di lavoro

1. **Nuovo Caso** - crea la cartella in `cases/`.
2. **Disegna** la geometria sul canvas (o passa in modalita' 3D e carica un `.stl`).
3. **Mesh** - gmsh genera la mesh, `gmshToFoam` la converte, `checkMesh` la verifica.
4. **Imposta** - scrive tutti i dizionari OpenFOAM con i parametri del pannello.
5. **Simula** - lancia il solver, in seriale o in parallelo su N processi.
6. **Residui** / **ParaView** - controlla la convergenza e visualizza i campi.

---

## Struttura del progetto

```
app.py              # server Flask e API REST
mesher.py           # scrittura dei dizionari OpenFOAM e della geometria gmsh
mesh_worker.py      # gmsh eseguito in subprocess (evita il problema dei signal thread)
of_runner.py        # esecuzione dei comandi OpenFOAM via bash MSYS2, gestione solver
benchmark_procs.py  # benchmark dello speedup al variare del numero di processi
start.bat           # avvio su Windows con controllo dipendenze
templates/index.html
static/js/app.js        # logica UI
static/js/canvas2d.js   # editor geometria 2D
static/css/style.css
cases/              # casi generati (non versionati)
```

---

## API

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET/POST | `/api/cases` | elenca / crea un caso |
| DELETE | `/api/cases/<name>` | elimina un caso |
| POST | `/api/upload-stl/<case>` | carica un `.stl` (modalita' 3D) |
| POST | `/api/mesh` | genera la mesh (2D gmsh o 3D snappyHexMesh) |
| POST | `/api/setup` | scrive i dizionari del caso |
| POST | `/api/solve/start` / `/stop` | avvia / arresta il solver |
| GET | `/api/solve/status` / `/stream` | log del solver (polling o SSE) |
| GET | `/api/residuals/<case>` | residui iniziali parsati dal log |
| POST | `/api/solve/paraview` | apre il caso in ParaView |

---

## Note

- I casi in `cases/` non sono versionati: sono dati generati e possono pesare diversi GB.
- OPENFOAM(R) e' un marchio registrato di OpenCFD Ltd. Questo progetto non e' affiliato
  ne' approvato da OpenCFD Ltd.

## Autore

Matteo Faggian - Ingegneria Aerospaziale, Universita' di Padova.
