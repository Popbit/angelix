#include <iostream>
#include <sstream>
#include <fstream>

#include "../AngelixCommon.h"


std::string isBuggy(int beginLine, int beginColumn, int endLine, int endColumn) {
  std::string line;
  std::string suspiciousFile(getenv("ANGELIX_PATCH"));
  std::ifstream infile(suspiciousFile);
  int curBeginLine, curBeginColumn, curEndLine, curEndColumn;
  while (std::getline(infile, line)) {
    std::istringstream iss(line);
    iss >> curBeginLine >> curBeginColumn >> curEndLine >> curEndColumn;
    std::getline(infile, line);
    if (curBeginLine   == beginLine &&
        curBeginColumn == beginColumn &&
        curEndLine     == endLine &&
        curEndColumn   == endColumn) {
      return line;
    }
  }
  return NULL;
}


class ConditionalHandler : public MatchFinder::MatchCallback {
public:
  ConditionalHandler(Rewriter &Rewrite) : Rewrite(Rewrite) {}

  virtual void run(const MatchFinder::MatchResult &Result) {
    if (const Expr *expr = Result.Nodes.getNodeAs<clang::Expr>("repairable")) {
      SourceManager &srcMgr = Rewrite.getSourceMgr();

      SourceRange expandedLoc = getExpandedLoc(expr, srcMgr);

      unsigned beginLine = srcMgr.getSpellingLineNumber(expandedLoc.getBegin());
      unsigned beginColumn = srcMgr.getSpellingColumnNumber(expandedLoc.getBegin());
      unsigned endLine = srcMgr.getSpellingLineNumber(expandedLoc.getEnd());
      unsigned endColumn = srcMgr.getSpellingColumnNumber(expandedLoc.getEnd());

      std::string replacement = NULL;

      if ((replacement = isBuggy(beginLine, beginColumn, endLine, endColumn)) == NULL) {
        return;
      }

      std::cout << beginLine << " " << beginColumn << " " << endLine << " " << endColumn << "\n"
                << "<   " << toString(expr) << "\n"
                << ">   " << replacement << "\n";

      Rewrite.ReplaceText(expandedLoc, replacement);
    }
  }

private:
  Rewriter &Rewrite;
};


class MyASTConsumer : public ASTConsumer {
public:
  MyASTConsumer(Rewriter &R) : HandlerForConditional(R) {

    Matcher.addMatcher(RepairableIfCondition, &HandlerForConditional);
  }

  void HandleTranslationUnit(ASTContext &Context) override {
    Matcher.matchAST(Context);
  }

private:
  ConditionalHandler HandlerForConditional;
  MatchFinder Matcher;
};


class ApplyPatchAction : public ASTFrontendAction {
public:
  ApplyPatchAction() {}

  void EndSourceFileAction() override {
    FileID ID = TheRewriter.getSourceMgr().getMainFileID();
    if (INPLACE_MODIFICATION) {
      TheRewriter.overwriteChangedFiles();
    } else {
      TheRewriter.getEditBuffer(ID).write(llvm::outs());
    }
  }

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI, StringRef file) override {
    TheRewriter.setSourceMgr(CI.getSourceManager(), CI.getLangOpts());
    return llvm::make_unique<MyASTConsumer>(TheRewriter);
  }

private:
  Rewriter TheRewriter;
};


// Apply a custom category to all command-line options so that they are the only ones displayed.
static llvm::cl::OptionCategory MyToolCategory("angelix options");


int main(int argc, const char **argv) {
  // CommonOptionsParser constructor will parse arguments and create a
  // CompilationDatabase.  In case of error it will terminate the program.
  CommonOptionsParser OptionsParser(argc, argv, MyToolCategory);

  // We hand the CompilationDatabase we created and the sources to run over into the tool constructor.
  ClangTool Tool(OptionsParser.getCompilations(), OptionsParser.getSourcePathList());

  return Tool.run(newFrontendActionFactory<ApplyPatchAction>().get());
}