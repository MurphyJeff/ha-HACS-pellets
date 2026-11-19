# Pellets — intégration Home Assistant

Gestion des pellets pour un poêle à granulés (testé avec l'intégration `aguaiot`, cartes Micronova) :

- stocks fournisseur et maison, avec suivi du sac entamé ;
- estimation du réservoir avec une marge d'erreur, et heure prévue où il sera vide ;
- consommation calculée par palier de puissance, qui se **calibre seule** à chaque remplissage « au plein » ou constat de réservoir vide ;
- journal d'événements **rejoué** : corriger ou supprimer une entrée, même ancienne, recalcule tout.

## Installation
- **HACS** : ajouter ce dépôt comme dépôt personnalisé (type intégration), installer « Pellets », puis redémarrer.
- **Manuelle** : copier `custom_components/pellets` dans `/config/custom_components/`, puis redémarrer.

Ensuite : Paramètres > Appareils et services > Ajouter > **Pellets**. Un assistant en 3 étapes se lance :
1. **🔥 Ton poêle** : les entités sont détectées automatiquement (aguaiot, Micronova…), il suffit de vérifier.
2. **⚙️ Ton réservoir et tes sacs** : capacité, poids d'un sac, et en option les temps de vis (menu E9, P06 à P10). Laisse le facteur à 1, il s'apprend seul.
3. **📦 Ton point de départ** : sacs à la maison, sacs chez le fournisseur, prix du sac, niveau du réservoir. L'historique de chauffe de HA est repris pour apprendre dès le début.

## Réglages (Configurer)
- **Réglages du modèle** :
  - capacité du réservoir et poids d'un sac ;
  - facteur de départ ;
  - seuils d'alerte ;
  - temps de vis par palier, à relever dans le menu E9 du poêle (P06 = P1, P07 = P2…).
- **Corriger le journal** : choisir une entrée, puis modifier la date, la quantité ou l'indicateur « plein », ou la supprimer.

## Suivi de la consommation et de la combustion
- **Consommation aujourd'hui** (kg) : avec la veille et les 7 derniers jours en attributs.
- **Énergie des pellets** (kWh, à 4,8 kWh/kg) : à ajouter au tableau de bord Énergie de HA.
- **Temps de combustion aujourd'hui** (h) : détail par palier, pour la veille et pour les 7 derniers jours.
- **Allumages aujourd'hui** : pour la veille et les 7 derniers jours. Un changement de palier n'est pas compté comme un allumage.
- **Dernière combustion** (h) : début, fin, kg et palier moyen.
- **Coût aujourd'hui** (€) : au prix moyen du sac, avec la veille.

## Services
| Service | Usage |
|---|---|
| `pellets.fill` | Remplissage : `bags` (1,15 = un sac + 15 %) et `full` (vrai si le réservoir est plein après) |
| `pellets.mark_empty` | Constat de réservoir vide (sert d'ancrage pour la calibration) |
| `pellets.purchase` | Achat : `bags`, `total_eur` ou `price_per_bag`, `location` (`supplier` ou `home`) |
| `pellets.transfer` | Sacs ramenés du fournisseur, avec `supplier_remaining` (le stock noté sur la fiche fait foi) |
| `pellets.inventory` | Recalage sur un comptage : `home_bags`, `supplier_bags`, `reservoir_kg` |
| `pellets.undo_last`, `pellets.edit_entry`, `pellets.delete_entry` | Corrections |
| `pellets.import_history` | Reconstruit les périodes de combustion depuis l'historique HA |
| `pellets.get_journal` | Renvoie le journal |

Tous les services acceptent `when` pour une saisie rétroactive.

## Événements `pellets_event`
- `entry_added` : une saisie vient d'être enregistrée.
- `reservoir_low` : le réservoir passe sous le seuil d'alerte.
- `empty_detected` : alarme du poêle suivie d'un arrêt. Une **confirmation est demandée**, car l'alarme peut être fausse.
- `possible_empty` : arrêt du poêle alors que l'estimation est presque à zéro.
- `home_stock_low` : le stock maison passe sous le seuil d'alerte.
- `warning` : écart avec la fiche fournisseur, calibration aberrante ou remplissage incohérent.

Le blueprint `blueprints/automation/pellets/pellets_notifications.yaml` transforme ces événements en notifications :
- plage silencieuse la nuit ;
- alerte du soir si le réservoir risque de se vider pendant la nuit ;
- boutons « Annuler » et « Rempli au plein » dans les notifications ;
- appuis longs d'un interrupteur IKEA E1743 : « O » long = +1 sac, « I » long = plein.

## Modèle
- **Formule** : `kg = facteur × Σ heures_au_palier × débit_de_base[palier]`.
- **Débit de base** : proportionnel au temps de vis du palier (P1 calé sur 0,93 kg/h, la valeur de la fiche), sinon la fiche constructeur (0,93 / 1,35 / 1,77 / 2,18 / 2,6 kg/h).
- **Ajustement du facteur** à chaque ancrage :
  - gain de 0,5, puis 0,33, puis 0,3 au minimum ;
  - une valeur aberrante (écart de plus de 40 % après 2 calibrations) est ignorée et signalée ;
  - un inventaire visuel compte pour un demi-ancrage.

## Tests
```
pip install pytest-homeassistant-custom-component
pytest
```
