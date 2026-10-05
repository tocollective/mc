extern let cAdjust(value: Word): Word

let mAdd(left: Word, right: Word): Word {
    return cAdjust(left + right)
}

export { mAdd }
