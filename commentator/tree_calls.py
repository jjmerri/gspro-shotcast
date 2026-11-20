"""The stock lines called in flight as a ball hits a tree, per commentary language (config.LANGUAGES):
(in the trees, low). A low hit is a bush or the lowest branches. Each is picked at random, so there are
plenty to keep a round from repeating itself."""

TREE_CALLS = (
    "Oh, that's into the trees!", "That's caught a branch!", "Oh no, it's clipped the tree!",
    "Ooh, that's found the trees!", "Timber! That's in the branches!", "That's rattled the leaves!",
    "Ooh, right against a branch!", "That's clattered into the trees!", "Ooh, straight into the canopy!",
    "That one's hit timber!", "Oh dear, that's found the woods!", "Oh, that's thumped a branch!",
    "That's taken a branch on the way!", "Ooh, the trees have got a piece of it!",
    "Oh, that one clipped the leaves!", "Oh no, that's flown right into the branches!",
    "That's brushed the treetops!", "Oof, a tree's got in the way!", "Oh, that's found the foliage!",
    "Oh, it's hit the trees, that's unlucky!",
)
TREE_LOW_CALLS = (
    "Oh, that's clipped something low!", "That's into the bushes!", "Ooh, caught the shrubbery!",
    "That's clipped the undergrowth!", "Oh, it's found the scrub!", "That's hit something down low!",
    "Oh no, into the brush!", "That's nicked a bush on the way!", "Ooh, that's rattled the bushes!",
    "Caught the low stuff there!",
)

TREE_CALLS_BY_LANGUAGE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "en": (TREE_CALLS, TREE_LOW_CALLS),
    "fr": ((
        "Oh, c'est dans les arbres !", "Il a touché une branche !", "Oh non, il a accroché l'arbre !",
        "Aïe, il a trouvé les arbres !", "Ouh, en plein dans les branches !", "Ça a fait trembler les feuilles !",
        "Oh, la balle heurte les branches !", "Il a percuté les arbres !", "Oh, droit dans le feuillage !",
        "Ça tape dans les branches !", "Oh là là, il a trouvé le bois !", "Ça claque dans le bois !",
        "Il a pris une branche au passage !", "Ouh, les arbres l'ont freinée !", "Oh, la balle frappe le feuillage !",
        "Oh non, la balle file droit dans les branches !", "Il a frôlé la cime des arbres !",
        "Aïe, un arbre s'est mis en travers !", "Oh, la balle effleure les branches !",
        "Oh, il touche les arbres, pas de chance !",
    ), (
        "Oh, il a accroché quelque chose au ras du sol !", "C'est dans les buissons !",
        "Ouh, il a accroché les arbustes !", "Il a touché les broussailles !", "Oh, il a trouvé les fourrés !",
        "Il a touché quelque chose tout en bas !", "Oh non, dans les taillis !",
        "Il a effleuré un buisson au passage !", "Ouh, en plein dans les buissons !",
        "Il a accroché la végétation basse !",
    )),
    "es": ((
        "¡Oh, se ha ido a los árboles!", "¡Ha tocado una rama!", "¡Oh no, ha golpeado el árbol!",
        "¡Uy, ha encontrado los árboles!", "¡Uf, de lleno en las ramas!", "¡Ha hecho temblar las hojas!",
        "¡Oh, la bola pega en las ramas!", "¡Se ha estrellado contra los árboles!",
        "¡Oh, a la copa de los árboles!", "¡Ha dado en la madera!", "¡Ay, va directa a los árboles!",
        "¡Ha chocado con las ramas!", "¡Vaya golpe contra esa rama!", "¡Uy, los árboles la han frenado!",
        "¡Oh, se mete en las hojas!", "¡Oh no, vuela directa a las ramas!", "¡Ha rozado las copas!",
        "¡Ay, un árbol se ha cruzado en el camino!", "¡Oh, roza las hojas!",
        "¡Oh, ha tocado los árboles, qué mala suerte!",
    ), (
        "¡Oh, ha tocado algo abajo!", "¡Se ha metido en los arbustos!", "¡Uy, ha pillado la maleza!",
        "¡Ha tocado los matorrales!", "¡Uy, roza el matorral!", "¡Ha tocado algo a ras de suelo!",
        "¡Oh no, entre los matojos!", "¡Ha rozado un arbusto al pasar!", "¡Uy, sacude los arbustos!",
        "¡Ha pillado la vegetación baja!",
    )),
    "de": ((
        "Oh, der ist in den Bäumen!", "Der hat einen Ast erwischt!", "Oh nein, der hat den Baum getroffen!",
        "Ui, mitten rein in die Bäume!", "Uff, voll in die Äste!", "Da rascheln die Blätter!",
        "Oh, der prallt gegen einen Ast!", "Der kracht in die Bäume!", "Oh, direkt in die Baumkronen!",
        "Der hat Holz getroffen!", "Oje, der ist im Wald!", "Der klatscht in die Äste!",
        "Der streift einen Ast!", "Uh, die Bäume bremsen ihn!", "Oh, der klatscht ins Laub!",
        "Oh nein, der fliegt genau in die Äste!", "Der streift die Baumwipfel!",
        "Autsch, da steht ein Baum im Weg!", "Oh, der streift die Blätter!",
        "Oh, der trifft die Bäume, Pech!",
    ), (
        "Oh, der hat unten etwas erwischt!", "Der ist in den Büschen!", "Uh, der hat das Gestrüpp erwischt!",
        "Der hat das Unterholz getroffen!", "Oh, der ist im Dickicht!", "Da war was Niedriges im Weg!",
        "Oh nein, ins Gebüsch!", "Der streift im Vorbeiflug einen Busch!", "Uh, der rauscht in die Büsche!",
        "Der hat die Sträucher erwischt!",
    )),
    "it": ((
        "Oh, dritta tra gli alberi!", "Ha preso un ramo!", "Oh no, ha colpito l'albero!",
        "Uh, ha trovato gli alberi!", "Uff, in pieno tra i rami!", "Ha fatto tremare le foglie!",
        "Oh, la palla urta i rami!", "Si è schiantata contro gli alberi!", "Oh, in mezzo alle chiome!",
        "Ha colpito il legno!", "Ahi ahi, sbatte contro gli alberi!", "Che botta tra i rami!",
        "Ha toccato un ramo al passaggio!", "Uh, gli alberi l'hanno frenata!", "Oh, colpisce le foglie!",
        "Oh no, vola dritta nei rami!", "Ha sfiorato le cime degli alberi!",
        "Ahi, un albero si è messo di mezzo!", "Oh, sfiora le foglie!",
        "Oh, ha toccato gli alberi, che sfortuna!",
    ), (
        "Oh, ha preso qualcosa in basso!", "Oh, tocca i cespugli!", "Uh, ha preso la sterpaglia!",
        "Ha toccato il sottobosco!", "Oh, sfiora la macchia!", "Ha colpito qualcosa rasoterra!",
        "Oh no, tra gli arbusti!", "Ha sfiorato un cespuglio al passaggio!", "Uh, scuote i cespugli!",
        "Ha preso la vegetazione bassa!",
    )),
    "pt": ((
        "Ah, foi para as árvores!", "Pegou num galho!", "Ah não, bateu na árvore!", "Opa, achou as árvores!",
        "Xi, em cheio nos galhos!", "Fez as folhas tremerem!", "Ah, esbarrou nos galhos!",
        "Se chocou contra as árvores!", "Ah, direto na copa das árvores!", "Bateu na madeira!",
        "Ih, foi direto nas árvores!", "Que pancada nos galhos!", "Tocou num galho no caminho!",
        "Opa, a bola bateu nos galhos!", "Ah, bateu nas folhas!", "Ah não, voou direto para os galhos!",
        "Raspou nas copas!", "Ai, uma árvore entrou no caminho!", "Ih, raspou nas folhas!",
        "Ah, bateu nas árvores, que azar!",
    ), (
        "Ah, pegou em algo baixo!", "Bateu nos arbustos!", "Opa, pegou no mato!",
        "Bateu na vegetação rasteira!", "Ah, achou o matagal!", "Pegou em algo rente ao chão!",
        "Ah não, nas moitas!", "Raspou num arbusto no caminho!", "Opa, sacudiu os arbustos!",
        "Pegou numa moita ali embaixo!",
    )),
    "nl": ((
        "Oh, die zit in de bomen!", "Die raakt een tak!", "Oh nee, die tikt de boom aan!",
        "Oei, zo de bomen in!", "Pff, vol in de takken!", "Daar ritselen de bladeren!",
        "Oh, die botst tegen een tak!", "Die knalt de bomen in!", "Oh, recht de boomkruinen in!",
        "Die raakt hout!", "Oei oei, die zit in het bos!", "Die klapt tegen de takken!",
        "Oei, vol tegen een tak!", "Oei, de bomen remmen hem af!", "Oh, die tikt de bladeren aan!",
        "Oh nee, die vliegt zo de takken in!", "Die scheert langs de boomtoppen!",
        "Au, daar staat een boom in de weg!", "Oh, die schampt de bladeren!", "Oh, die raakt de bomen, pech!",
    ), (
        "Oh, die raakt daar beneden iets!", "Die zit in de struiken!", "Oei, die pakt het struikgewas!",
        "Die raakt het kreupelhout!", "Oh, die zit in het dichte groen!", "Die raakt iets vlak bij de grond!",
        "Oh nee, de bosjes in!", "Die schampt onderweg een struik!", "Oei, die ritselt in de struiken!",
        "Die pakt de lage begroeiing!",
    )),
}
